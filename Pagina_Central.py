"""
Anjun Brasil - Monitoramento de Pontualidade
=============================================
Streamlit app para o supervisor subir a base semanal (modelo cru do
sistema, mesmas 86 colunas de "monitoramento_da_pontualidade_de_pedido")
e acompanhar o painel de pontualidade por supervisor/ponto.

A tabela public.base no Supabase tem um trigger que calcula automaticamente
(week, leed_time, hub_conexao, supervisor etc.) assim que a linha entra --
este app só precisa inserir as colunas cruas.
"""

import json
import os
import re
from datetime import datetime, timedelta, date

import pandas as pd
import psycopg2
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from psycopg2.extras import execute_values

# ---------------------------------------------------------------------
# Identidade visual Anjun Brasil (cores extraídas direto da logo)
# ---------------------------------------------------------------------
ANJUN_GREEN = "#009946"
ANJUN_GREEN_DARK = "#00753A"
ANJUN_RED = "#E80115"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(BASE_DIR, "assets", "anjun_logo.png")
MAPPING_PATH = os.path.join(BASE_DIR, "config", "column_mapping_base.json")

st.set_page_config(
    page_title="Anjun Brasil | Monitoramento de Pontualidade",
    page_icon=LOGO_PATH,
    layout="wide",
)

st.markdown(
    f"""
    <style>
    .anjun-header {{
        display: flex;
        align-items: center;
        gap: 1rem;
        padding: 0.5rem 0 1.5rem 0;
        border-bottom: 3px solid {ANJUN_GREEN};
        margin-bottom: 1.5rem;
    }}
    .anjun-header h1 {{
        color: {ANJUN_GREEN_DARK};
        font-size: 1.6rem;
        margin: 0;
    }}
    .anjun-header p {{
        color: #555;
        margin: 0;
        font-size: 0.9rem;
    }}
    div[data-testid="stMetric"] {{
        background-color: #F0F7F2;
        border: 1px solid #E0EDE4;
        border-radius: 10px;
        padding: 0.8rem 1rem;
    }}
    div[data-testid="stMetricValue"] {{
        color: {ANJUN_GREEN_DARK};
    }}
    .stTabs [data-baseweb="tab"] {{
        font-weight: 600;
    }}
    .stButton > button[kind="primary"] {{
        background-color: {ANJUN_GREEN};
        border-color: {ANJUN_GREEN};
    }}
    .stButton > button[kind="primary"]:hover {{
        background-color: {ANJUN_GREEN_DARK};
        border-color: {ANJUN_GREEN_DARK};
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

col_logo, col_title = st.columns([1, 6])
with col_logo:
    st.image(LOGO_PATH, width=140)
with col_title:
    st.markdown(
        """
        <div style="padding-top: 0.6rem;">
            <h1 style="color:#00753A; margin-bottom:0;">Monitoramento de Pontualidade</h1>
            <p style="color:#555; margin-top:0.2rem;">Anjun Brasil &middot; painel operacional por supervisor</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------
# Conexão com o banco (Supabase / Postgres)
# ---------------------------------------------------------------------
@st.cache_resource
def get_connection():
    cfg = st.secrets["supabase"]
    conn = psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["dbname"],
        user=cfg["user"],
        password=cfg["password"],
        sslmode="require",
    )
    # Importante: sem isso, toda consulta (inclusive um simples SELECT)
    # deixa uma transação aberta ("idle in transaction") até alguém
    # chamar commit(). Isso segura locks e pode travar até um ALTER TABLE
    # no banco, mesmo sem nenhuma operação de escrita pendente.
    conn.autocommit = True
    return conn


def run_query(sql: str, params: dict | None = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        return pd.read_sql(sql, conn, params=params)
    except Exception:
        conn.rollback()
        raise


def run_write(sql: str, params: dict | None = None) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(sql, params)


@st.cache_data
def load_column_mapping() -> list[dict]:
    with open(MAPPING_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------
# Utilitarios de conversao / upload
# ---------------------------------------------------------------------
def to_text(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (pd.Timestamp, datetime)):
        return str(v)
    if isinstance(v, pd.Timedelta):
        return str(v)
    return str(v)


def parse_periodo_from_filename(filename):
    m = re.search(r"(\d{1,2}_\d{1,2}_A_\d{1,2}_\d{1,2})", filename, re.IGNORECASE)
    return m.group(1) if m else filename.rsplit(".", 1)[0]


def validar_colunas(header_row, mapping):
    """Compara posicionalmente (índice a índice), não por nome — assim
    colunas com o MESMO texto de cabeçalho repetido (ex: 'Centro real de
    chegada' aparece 3x no arquivo real) não quebram a validação."""
    esperadas = [m["original"] for m in mapping]
    recebidas = ["" if h is None else str(h) for h in header_row]
    if len(recebidas) < len(esperadas):
        recebidas = recebidas + [""] * (len(esperadas) - len(recebidas))
    diffs = [
        (i, esperadas[i], recebidas[i])
        for i in range(len(esperadas))
        if recebidas[i] != esperadas[i]
    ]
    extras = [h for h in recebidas[len(esperadas):] if h]
    return diffs, extras


def insert_base(data_df, mapping, arquivo_origem, periodo, batch_size=1000, progress_cb=None):
    """data_df: DataFrame lido com header=None (colunas 0..N-1 posicionais,
    sem a linha de cabeçalho). Acessa cada valor por posição (m['idx']),
    nunca por nome de coluna -- evita o bug de cabeçalhos duplicados.

    Faz UPSERT por numero_do_waybill: se o pedido ja existe na base (outra
    carga, ou reenvio do mesmo arquivo), atualiza a linha em vez de duplicar.
    O trigger recalcula as colunas auxiliares nos dois casos (INSERT e
    UPDATE), entao nunca fica dado calculado desatualizado."""
    slugs = [m["slug"] for m in mapping]
    idxs = [m["idx"] for m in mapping]
    cols_sql = ["arquivo_origem", "periodo_referencia", "row_num"] + slugs
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols_sql if c != "numero_do_waybill")
    set_clause += ", data_importacao = now()"
    insert_sql = (
        "INSERT INTO public.base (" + ", ".join(cols_sql) + ") VALUES %s "
        "ON CONFLICT (numero_do_waybill) DO UPDATE SET " + set_clause
    )

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.base")
        antes = cur.fetchone()[0]

    total = 0
    n = len(data_df)
    with conn.cursor() as cur:
        for start in range(0, n, batch_size):
            chunk = data_df.iloc[start:start + batch_size]
            rows = []
            for i, row in chunk.iterrows():
                vals = [arquivo_origem, periodo, i + 1] + [to_text(row[idx]) for idx in idxs]
                rows.append(tuple(vals))
            execute_values(cur, insert_sql, rows)
            conn.commit()
            total += len(rows)
            if progress_cb:
                progress_cb(total / n)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.base")
        depois = cur.fetchone()[0]

    novos = depois - antes
    atualizados = total - novos
    return total, novos, atualizados


def upsert_supervisor(ponto, supervisor):
    run_write(
        """
        INSERT INTO public.supervisores (ponto, supervisor)
        VALUES (%(ponto)s, %(supervisor)s)
        ON CONFLICT (ponto) DO UPDATE
          SET supervisor = EXCLUDED.supervisor, atualizado_em = now()
        """,
        {"ponto": ponto, "supervisor": supervisor},
    )


def recalcular_pendentes(ponto=None):
    conn = get_connection()
    with conn.cursor() as cur:
        if ponto:
            cur.execute(
                "UPDATE public.base SET id = id WHERE ponto_de_entrega = %s AND supervisor IS NULL",
                (ponto,),
            )
        else:
            cur.execute("UPDATE public.base SET id = id WHERE supervisor IS NULL")
        n = cur.rowcount
    conn.commit()
    return n


# ---------------------------------------------------------------------
# Abas
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# Painel Logístico: mapeamento etapa -> coluna calculada na base
# (mesmas 9 etapas do funil já usadas no ETL local do time; aqui vêm
# prontas do trigger, calculadas por subtração de TIMESTAMP nativa do
# Postgres -- sem a ambiguidade de epoch do Excel.)
# ---------------------------------------------------------------------
ETAPAS = [
    ("tempo_criacao_x_hub_01_perus", "Criação → HUB1 / 创建 → HUB1"),
    ("tempo_em_hub1_perus", "Parado no HUB1 / HUB1 停留"),
    ("tempo_trans_hub1_x_hub2_perus_hub2", "Trânsito HUB1 → HUB2 / HUB1 → HUB2 转运"),
    ("tempo_em_hub2_hub2", "Parado no HUB2 / HUB2 停留"),
    # Removidas: "Trânsito HUB2 → HUB3" e "Parado no HUB3" -- menos de 1% dos
    # pacotes passa por um 3o hub, entao ficavam quase sempre zeradas e só
    # ocupavam espaço na tabela/gráfico sem agregar sinal.
    ("tempo_trans_hub3_hub2_x_last_mile_hub3_hub2_dsp", "Trânsito HUB3/HUB2 → Last Mile / HUB3/HUB2 → 末端 转运"),
    ("parado_em_lm_dsp", "Parado no Last Mile (DSP) / 末端网点(DSP) 停留"),
    ("tempo_em_rota_de_entrega", "Em rota de entrega → Finalização / 派送途中 → 签收完成"),
]
COR_AZUL = "#1f77b4"
COR_LARANJA = "#ff7f0e"
COR_VERDE = "#2ca02c"
COR_VERMELHA = "#C00000"
COR_CINZA = "#B0B0B0"


def fmt_duracao(segundos):
    """Converte segundos em 'Dd HH:MM:SS', igual ao formato do Excel original."""
    if segundos is None or pd.isna(segundos):
        return "—"
    segundos = int(segundos)
    dias, resto = divmod(segundos, 86400)
    horas, resto = divmod(resto, 3600)
    minutos, seg = divmod(resto, 60)
    if dias > 0:
        return f"{dias}d {horas:02d}:{minutos:02d}:{seg:02d}"
    return f"{horas:02d}:{minutos:02d}:{seg:02d}"


def fmt_horas_decimal(segundos):
    if segundos is None or pd.isna(segundos):
        return 0.0
    return round(segundos / 3600, 1)


def fmt_variacao(pct, inverso=False):
    """inverso=True => menor é melhor (lead time, atraso)."""
    if pct is None or pd.isna(pct):
        return "—"
    piorou = pct > 0 if inverso else pct < 0
    if abs(pct) < 0.5:
        return f"⚪ ~{pct:+.1f}%"
    seta = "▲" if pct > 0 else "▼"
    cor = "🔴" if piorou else "🟢"
    return f"{cor} {seta} {abs(pct):.1f}%"


def cor_variacao(v, inverso: bool):
    """inverso=True => menor é melhor (DSP, Finalização). inverso=False => maior é melhor (Volume)."""
    if pd.isna(v):
        return ""
    piorou = v > 0 if inverso else v < 0
    if abs(v) < 0.05:
        return "color: #757575"
    return "color: #C00000; font-weight:600" if piorou else "color: #2E7D32; font-weight:600"


tab_upload, tab_logistico, tab_painel, tab_supervisores = st.tabs(
    ["Upload da Base", "Painel Logístico", "Painel do Supervisor", "Supervisores"]
)

mapping = load_column_mapping()

# ===================== ABA 1: UPLOAD =====================
# ---------------------------------------------------------------------
# Consultas SQL do Painel Logístico
# ---------------------------------------------------------------------
STAGE_COLS = [c for c, _ in ETAPAS]


def compose_where(clauses):
    return ("WHERE " + " AND ".join(clauses)) if clauses else ""


@st.cache_data(ttl=600)
def get_distinct(col):
    df = run_query(f"SELECT DISTINCT {col} AS v FROM public.base WHERE {col} IS NOT NULL ORDER BY 1")
    return df["v"].tolist()


@st.cache_data(ttl=600)
def get_data_bounds(campo="criacao"):
    df = run_query(f"SELECT min({campo})::date AS mn, max({campo})::date AS mx FROM public.base")
    return df.iloc[0]["mn"], df.iloc[0]["mx"]


def _avg_epoch_exprs(cols):
    return ",\n".join(f"avg(EXTRACT(EPOCH FROM {c})) AS {c}" for c in cols)


def query_kpis(clauses, params):
    sql = f"""
        SELECT
          count(*) AS pacotes,
          avg(EXTRACT(EPOCH FROM leed_time)) AS leed_time_seg,
          {_avg_epoch_exprs(STAGE_COLS)},
          avg((motivo_da_ocorrencia IS NOT NULL AND btrim(motivo_da_ocorrencia) <> '')::int) * 100 AS pct_ocorrencia
        FROM public.base
        {compose_where(clauses)}
    """
    df = run_query(sql, params)
    return df.iloc[0] if len(df) else None


def query_group(group_col, clauses, params):
    sql = f"""
        SELECT
          {group_col} AS grupo,
          count(*) AS volumetria,
          avg(EXTRACT(EPOCH FROM leed_time)) AS leed_time_seg,
          {_avg_epoch_exprs(STAGE_COLS)}
        FROM public.base
        {compose_where(clauses + [f"{group_col} IS NOT NULL"])}
        GROUP BY {group_col}
    """
    return run_query(sql, params)


def query_top_weeks(clauses, params, n=2):
    sql = f"""
        SELECT DISTINCT week FROM public.base
        {compose_where(clauses + ["week IS NOT NULL"])}
        ORDER BY week DESC LIMIT {n}
    """
    df = run_query(sql, params)
    return sorted(df["week"].tolist())


def query_weekly_by_group(group_col, weeks, clauses, params):
    p2 = dict(params)
    p2["_weeks"] = tuple(weeks)
    sql = f"""
        SELECT
          {group_col} AS grupo, week,
          count(*) AS vol,
          avg(EXTRACT(EPOCH FROM parado_em_lm_dsp)) / 3600.0 AS dsp_h,
          avg(EXTRACT(EPOCH FROM tempo_em_rota_de_entrega)) / 3600.0 AS final_h
        FROM public.base
        {compose_where(clauses + [f"{group_col} IS NOT NULL", "week IN %(_weeks)s"])}
        GROUP BY {group_col}, week
    """
    return run_query(sql, p2)


def montar_comparativo_semanal(group_col, rotulo_grupo, clauses, params):
    weeks = query_top_weeks(clauses, params)
    if len(weeks) < 2:
        return None, None, None
    sem1, sem2 = weeks[-2], weeks[-1]
    long_df = query_weekly_by_group(group_col, [sem1, sem2], clauses, params)
    if long_df.empty:
        return None, sem1, sem2
    pivot = long_df.pivot(index="grupo", columns="week", values=["dsp_h", "final_h", "vol"])

    t = pd.DataFrame(index=pivot.index)
    t["Parado DSP (h)"] = pivot[("dsp_h", sem1)].round(1)
    t["Finalização (h)"] = pivot[("final_h", sem1)].round(1)
    t["Vol Sem1"] = pivot[("vol", sem1)].fillna(0).astype(int)
    t["Parado DSP Sem2 (h)"] = pivot[("dsp_h", sem2)].round(1)
    t["Finalização Sem2 (h)"] = pivot[("final_h", sem2)].round(1)
    t["Vol Sem2"] = pivot[("vol", sem2)].fillna(0).astype(int)
    t["Var. DSP (h)"] = (t["Parado DSP Sem2 (h)"] - t["Parado DSP (h)"]).round(1)
    t["Var. Final (h)"] = (t["Finalização Sem2 (h)"] - t["Finalização (h)"]).round(1)
    t["Var. Vol"] = (t["Vol Sem2"] - t["Vol Sem1"]).astype(int)
    t = t.reset_index().rename(columns={"grupo": rotulo_grupo})
    return t, sem1, sem2


def grafico_variacao_semanal(tabela, rotulo_grupo):
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(
        x=tabela[rotulo_grupo], y=tabela["Var. DSP (h)"], name="Var. DSP (h)",
        mode="lines+markers", line=dict(color=COR_AZUL, width=2), marker=dict(size=8),
        hovertemplate="Var. DSP: %{y:+.1f}h<extra></extra>",
    ), secondary_y=False)
    fig.add_trace(go.Scatter(
        x=tabela[rotulo_grupo], y=tabela["Var. Final (h)"], name="Var. Finalização (h)",
        mode="lines+markers", line=dict(color=COR_LARANJA, width=2), marker=dict(size=8),
        hovertemplate="Var. Finalização: %{y:+.1f}h<extra></extra>",
    ), secondary_y=False)
    fig.add_trace(go.Scatter(
        x=tabela[rotulo_grupo], y=tabela["Var. Vol"], name="Var. Volume (pacotes)",
        mode="lines+markers", line=dict(color=COR_VERDE, width=2, dash="dot"), marker=dict(size=8),
        hovertemplate="Var. Volume: %{y:+,}<extra></extra>",
    ), secondary_y=True)
    fig.add_hline(y=0, line_dash="dash", line_color=COR_CINZA, secondary_y=False)
    fig.update_layout(
        height=380, margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        title=f"Variação Sem1 → Sem2 por {rotulo_grupo}", hovermode="x unified",
    )
    fig.update_yaxes(title_text="Variação (horas)", secondary_y=False)
    fig.update_yaxes(title_text="Variação (pacotes)", secondary_y=True)
    if tabela[rotulo_grupo].nunique() > 6:
        fig.update_xaxes(tickangle=-45)
    return fig


def render_upload():
    st.subheader("Subir arquivo semanal (modelo cru do sistema)")
    st.caption(
        f"Aceita o export padrão ({len(mapping)} colunas, mesmo modelo de "
        "monitoramento_da_pontualidade_de_pedido). O banco calcula "
        "sozinho week, leed time, hub e supervisor assim que a linha entra."
    )
    st.caption(
        "🔒 Protegido contra duplicidade: se um pedido (waybill) já existir na base "
        "— de um período que se sobrepõe, ou reenvio do mesmo arquivo — a linha é "
        "**atualizada**, não duplicada. Pode subir tranquilo mesmo sem ter certeza "
        "se aquele arquivo já foi enviado antes."
    )

    uploaded = st.file_uploader("Arquivo .xlsx", type=["xlsx"])

    if uploaded is not None:
        try:
            # header=None: le a planilha como grade bruta, colunas 0..N-1.
            # Evita que o pandas renomeie cabecalhos duplicados (ex: "Centro
            # real de chegada" aparece 3x no arquivo real) para .1/.2, o que
            # fazia a validacao e a extracao pegarem a coluna errada.
            raw_df = pd.read_excel(uploaded, sheet_name=0, header=None)
        except Exception as e:
            st.error(f"Não consegui ler o arquivo: {e}")
            return

        header_row = raw_df.iloc[0].tolist()
        data_df = raw_df.iloc[1:].reset_index(drop=True)

        diffs, extras = validar_colunas(header_row, mapping)

        if diffs or extras:
            st.error(
                "Esse arquivo não bate com o modelo esperado pela tabela "
                "`base`. Antes de subir, ajuste o schema do banco (fale com "
                "quem mantém o banco de dados)."
            )
            if diffs:
                st.write(f"**Colunas diferentes do esperado, por posição ({len(diffs)}):**")
                st.dataframe(
                    pd.DataFrame(diffs, columns=["posição", "esperado", "recebido"]),
                    use_container_width=True, hide_index=True,
                )
            if extras:
                st.write(f"**Colunas novas no fim do arquivo, não mapeadas ({len(extras)}):**")
                st.code("\n".join(extras))
            return

        st.success(f"Modelo confere: {len(data_df)} linhas, {len(header_row)} colunas.")
        preview = data_df.head(10).copy()
        preview.columns = [m["slug"] for m in mapping]
        st.dataframe(preview, use_container_width=True)

        col_a, col_b = st.columns(2)
        with col_a:
            periodo = st.text_input(
                "Período de referência",
                value=parse_periodo_from_filename(uploaded.name),
            )
        with col_b:
            st.text_input("Arquivo de origem", value=uploaded.name, disabled=True)

        if st.button("Confirmar e subir para o banco", type="primary"):
            progress = st.progress(0.0, text="Enviando...")

            def _cb(frac):
                progress.progress(min(frac, 1.0), text=f"Enviando... {frac:.0%}")

            n, novos, atualizados = insert_base(data_df, mapping, uploaded.name, periodo, progress_cb=_cb)
            progress.empty()
            st.success(
                f"{n} linhas processadas: **{novos} pedidos novos** inseridos, "
                f"**{atualizados} já existentes** atualizados (nenhum duplicado)."
            )
            st.balloons()
            st.cache_data.clear()

# ===================== ABA 2: PAINEL DO SUPERVISOR =====================
def render_logistico():
    st.subheader("Painel de Performance Logística / 物流绩效看板")
    st.caption(
        "Réplica e ampliação do pivot 'Din' original, agora direto do banco: "
        "tempo médio por etapa do funil, gargalo, ocorrências e comparativo semanal."
    )
    st.caption(
        "复刻并扩展原 'Din' 数据透视表，现直接从数据库读取：各环节平均时长、"
        "瓶颈环节、问题件情况及周度对比。"
    )

    with st.expander("Filtros", expanded=True):
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            f_cliente = st.multiselect("Nome do Cliente", get_distinct("nome_do_cliente"))
        with col2:
            f_merchant = st.multiselect("Nome do Cliente Merchant", get_distinct("nome_do_cliente_merchant"))
        with col3:
            f_ponto = st.multiselect("Ponto de Entrega", get_distinct("ponto_de_entrega"))
        with col4:
            f_uf = st.multiselect("UF", get_distinct("estado_do_destinatario"))

        col5, col6, col7 = st.columns([1, 1, 2])
        with col5:
            f_supervisor = st.multiselect("Supervisor", get_distinct("supervisor"))
        with col6:
            campo_periodo_label = st.selectbox(
                "Filtrar período por", ["Assinatura (Finalização)", "Criação"], index=0,
            )
            campo_periodo = "finalizacao" if campo_periodo_label.startswith("Assinatura") else "criacao"
        with col7:
            data_min, data_max = get_data_bounds(campo_periodo)
            if data_min and data_max:
                inicio_padrao = max(data_min, data_max - timedelta(days=29))
                periodo = st.date_input(
                    f"Período de {campo_periodo_label.lower()}", value=(inicio_padrao, data_max),
                    min_value=data_min, max_value=data_max,
                )
            else:
                periodo = None

        st.caption(
            "Ponto de Entrega vem preenchido direto do sistema (raw), sem depender de "
            "lookup na legenda — por isso é o filtro operacional recomendado."
        )
        st.caption(
            "「派送网点」直接来自系统原始数据，不依赖图例表查找 —— "
            "因此是推荐使用的运营筛选字段。"
        )
        st.caption(
            "Assinatura = quando o pacote foi finalizado/assinado. Criação = quando o "
            "pedido foi aberto (costuma ficar meses antes da assinatura)."
        )
        st.caption("签收 = 包裹完成/签收的时间。创建 = 订单开立的时间（通常比签收早几个月）。")

    # -- monta clausulas categoricas (reaproveitadas no periodo atual, anterior e semanal) --
    cat_clauses, params = [], {}
    if f_cliente:
        cat_clauses.append("nome_do_cliente = ANY(%(cliente)s)")
        params["cliente"] = f_cliente
    if f_merchant:
        cat_clauses.append("nome_do_cliente_merchant = ANY(%(merchant)s)")
        params["merchant"] = f_merchant
    if f_ponto:
        cat_clauses.append("ponto_de_entrega = ANY(%(ponto)s)")
        params["ponto"] = f_ponto
    if f_uf:
        cat_clauses.append("estado_do_destinatario = ANY(%(uf)s)")
        params["uf"] = f_uf
    if f_supervisor:
        cat_clauses.append("supervisor = ANY(%(supervisor)s)")
        params["supervisor"] = f_supervisor

    if periodo is None:
        st.info(f"Sem dados de {campo_periodo_label.lower()} na base ainda — suba um arquivo na aba Upload.")
        return

    if isinstance(periodo, tuple) and len(periodo) == 2:
        ini, fim = periodo
    else:
        ini, fim = data_min, data_max

    params["ini"] = ini
    params["fim"] = fim
    clauses_atual = cat_clauses + [f"{campo_periodo}::date BETWEEN %(ini)s AND %(fim)s"]

    duracao_dias = (fim - ini).days + 1
    fim_ant = ini - timedelta(days=1)
    ini_ant = fim_ant - timedelta(days=duracao_dias - 1)
    params["ini_ant"] = ini_ant
    params["fim_ant"] = fim_ant
    clauses_ant = cat_clauses + [f"{campo_periodo}::date BETWEEN %(ini_ant)s AND %(fim_ant)s"]

    kpi_atual = query_kpis(clauses_atual, params)
    if kpi_atual is None or not kpi_atual["pacotes"]:
        st.warning("Nenhum pedido corresponde aos filtros selecionados.")
        return
    kpi_ant = query_kpis(clauses_ant, params)
    tem_periodo_anterior = kpi_ant is not None and bool(kpi_ant["pacotes"])

    # -------------------- KPIs --------------------
    c1, c2, c3, c4 = st.columns(4)
    vol_atual = int(kpi_atual["pacotes"])
    delta_vol = None
    if tem_periodo_anterior:
        delta_vol = f"{vol_atual - int(kpi_ant['pacotes']):+,}".replace(",", ".")
    c1.metric("Pacotes no filtro / 筛选范围内件量", f"{vol_atual:,}".replace(",", "."), delta=delta_vol, delta_color="off")

    lt_atual = kpi_atual["leed_time_seg"]
    delta_lt = None
    if tem_periodo_anterior and kpi_ant["leed_time_seg"]:
        delta_lt = f"{(lt_atual - kpi_ant['leed_time_seg']) / kpi_ant['leed_time_seg'] * 100:+.1f}%"
    c2.metric("Lead time médio / 平均时效", fmt_duracao(lt_atual), delta=delta_lt, delta_color="inverse")

    etapa_gargalo_col = max(STAGE_COLS, key=lambda c: kpi_atual[c] or 0)
    etapa_gargalo_nome = dict(ETAPAS)[etapa_gargalo_col]
    c3.metric("Maior gargalo (etapa) / 最大瓶颈环节", etapa_gargalo_nome)
    c3.caption(fmt_duracao(kpi_atual[etapa_gargalo_col]) + " em média / 平均")

    pct_oc = kpi_atual["pct_ocorrencia"] or 0.0
    delta_oc = None
    if tem_periodo_anterior:
        delta_oc = f"{pct_oc - (kpi_ant['pct_ocorrencia'] or 0.0):+.1f} p.p."
    c4.metric("Pacotes com ocorrência / 问题件占比", f"{pct_oc:.1f}%", delta=delta_oc, delta_color="inverse")

    if tem_periodo_anterior:
        st.caption(
            f"↕️ Comparado com o período anterior de mesma duração "
            f"({ini_ant:%d/%m} a {fim_ant:%d/%m}) · seta verde = melhora, vermelha = piora"
        )
        st.caption(f"↕️ 与相同天数的上一周期对比（{ini_ant:%d/%m} 至 {fim_ant:%d/%m}）· 绿色=改善，红色=恶化")
    else:
        st.caption("ℹ️ Sem histórico suficiente antes do período selecionado para comparar.")
        st.caption("ℹ️ 所选周期之前没有足够的历史数据可供对比。")

    st.divider()

    # -------------------- Tabela por UF --------------------
    st.subheader("Tempo médio por etapa — UF / 各环节平均时长 — 按州（UF）")
    agrupado_uf = query_group("estado_do_destinatario", clauses_atual, params).set_index("grupo")
    agrupado_uf.loc["Total Geral"] = {
        "volumetria": vol_atual, "leed_time_seg": lt_atual,
        **{c: kpi_atual[c] for c in STAGE_COLS},
    }

    lt_prev_por_uf = {}
    lt_prev_total = None
    if tem_periodo_anterior:
        prev_uf = query_group("estado_do_destinatario", clauses_ant, params).set_index("grupo")
        lt_prev_por_uf = prev_uf["leed_time_seg"].to_dict()
        lt_prev_total = kpi_ant["leed_time_seg"]

    def variacao_lead_time(chave, lt_atual_v, total=False):
        lt_prev = lt_prev_total if total else lt_prev_por_uf.get(chave)
        if lt_prev is None or pd.isna(lt_prev) or lt_prev == 0:
            return None
        return (lt_atual_v - lt_prev) / lt_prev * 100

    tabela_uf = pd.DataFrame(index=agrupado_uf.index)
    for c, nome in ETAPAS:
        tabela_uf[nome] = agrupado_uf[c].apply(fmt_duracao)
    tabela_uf["Tendência (Lead Time)"] = [
        fmt_variacao(
            variacao_lead_time(uf, agrupado_uf.loc[uf, "leed_time_seg"], total=(uf == "Total Geral")),
            inverso=True,
        )
        for uf in agrupado_uf.index
    ]
    tabela_uf["Volumetria"] = agrupado_uf["volumetria"].astype(int).apply(lambda x: f"{x:,}".replace(",", "."))

    tabela_uf_display = tabela_uf.T
    tabela_uf_display.index.name = "Etapa / 环节"
    st.dataframe(tabela_uf_display, use_container_width=True)
    if tem_periodo_anterior:
        st.caption("🟢 lead time caiu (melhorou) · 🔴 lead time subiu (piorou) · ⚪ variação menor que 0,5%")
        st.caption("🟢 时效缩短（改善）· 🔴 时效延长（恶化）· ⚪ 变化小于0.5%")

    st.divider()

    # -------------------- Tabela por Ponto de Entrega (IATA) --------------------
    st.subheader("Tempo de entrega por Ponto de Entrega (IATA) / 按派送网点（IATA）划分的时效")
    agrupado_iata = query_group("ponto_de_entrega", clauses_atual, params).set_index("grupo")
    agrupado_iata = agrupado_iata.sort_values("leed_time_seg", ascending=False)

    lt_prev_por_iata = {}
    if tem_periodo_anterior:
        prev_iata = query_group("ponto_de_entrega", clauses_ant, params).set_index("grupo")
        lt_prev_por_iata = prev_iata["leed_time_seg"].to_dict()

    tabela_iata = pd.DataFrame(index=agrupado_iata.index)
    tabela_iata["UF"] = [p.split("-")[0] if p else "" for p in agrupado_iata.index]
    tabela_iata["Lead Time Médio"] = agrupado_iata["leed_time_seg"].apply(fmt_duracao)
    tabela_iata["Tendência (Lead Time)"] = [
        fmt_variacao(
            (lambda lp: (agrupado_iata.loc[i, "leed_time_seg"] - lp) / lp * 100
             if lp and not pd.isna(lp) else None)(lt_prev_por_iata.get(i)),
            inverso=True,
        )
        for i in agrupado_iata.index
    ]
    tabela_iata["Volumetria"] = agrupado_iata["volumetria"].astype(int).apply(lambda x: f"{x:,}".replace(",", "."))
    tabela_iata.index.name = "Ponto de Entrega"
    st.dataframe(tabela_iata, use_container_width=True)
    st.caption("Ordenado do pior (maior lead time) pro melhor.")
    st.caption("按时效从差到好排序（时效最长的排最前）。")

    st.divider()

    # -------------------- Gargalo + Motivos --------------------
    colA, colB = st.columns([3, 2])
    with colA:
        st.subheader("Onde o tempo se concentra (média geral, em horas) / 时间集中在哪个环节（整体平均，小时）")
        medias_h = [fmt_horas_decimal(kpi_atual[c]) for c in STAGE_COLS]
        nomes_etapas = [n for _, n in ETAPAS]
        cores_gargalo = [COR_VERMELHA if n == etapa_gargalo_nome else COR_AZUL for n in nomes_etapas]
        fig = go.Figure(go.Bar(
            x=medias_h, y=nomes_etapas, orientation="h", marker_color=cores_gargalo,
            text=[f"{v:.1f}h" for v in medias_h], textposition="outside",
        ))
        fig.update_layout(height=400, margin=dict(l=10, r=60, t=10, b=10), xaxis_title="Horas (média)")
        fig.update_yaxes(autorange="reversed")
        st.plotly_chart(fig, use_container_width=True)
        st.caption(f"🔴 {etapa_gargalo_nome} é a etapa que mais consome tempo no filtro atual.")
        st.caption(f"🔴 {etapa_gargalo_nome} 是当前筛选条件下耗时最长的环节。")

    with colB:
        st.subheader("Motivos de ocorrência (top 8) / 问题件原因（前8位）")
        motivos_df = run_query(
            f"""
            SELECT motivo_da_ocorrencia AS motivo, count(*) AS n
            FROM public.base
            {compose_where(clauses_atual + ["motivo_da_ocorrencia IS NOT NULL", "btrim(motivo_da_ocorrencia) <> ''"])}
            GROUP BY motivo_da_ocorrencia ORDER BY n DESC LIMIT 8
            """,
            params,
        )
        if motivos_df.empty:
            st.info("Nenhuma ocorrência registrada no filtro atual.")
        else:
            fig2 = go.Figure(go.Bar(
                x=motivos_df["n"], y=motivos_df["motivo"], orientation="h", marker_color=COR_VERMELHA,
                text=[f"{v:,}".replace(",", ".") for v in motivos_df["n"]], textposition="outside",
            ))
            fig2.update_layout(height=400, margin=dict(l=10, r=60, t=10, b=10), xaxis_title="Ocorrências")
            fig2.update_yaxes(autorange="reversed")
            st.plotly_chart(fig2, use_container_width=True)

    st.divider()

    # -------------------- Volumetria e Lead Time por UF --------------------
    st.subheader("Volumetria e Lead Time médio por UF / 各州（UF）件量与平均时效")
    por_uf = agrupado_uf.drop(index="Total Geral", errors="ignore").reset_index().rename(columns={"grupo": "uf"})

    colC, colD = st.columns(2)
    with colC:
        por_uf_vol = por_uf.sort_values("volumetria", ascending=False)
        fig3 = go.Figure(go.Bar(
            x=por_uf_vol["uf"], y=por_uf_vol["volumetria"], marker_color=COR_AZUL,
            text=[f"{v:,}".replace(",", ".") for v in por_uf_vol["volumetria"]], textposition="outside",
        ))
        fig3.update_layout(title="Volumetria por UF", height=350, margin=dict(l=10, r=10, t=40, b=30), yaxis_title="Pacotes")
        st.plotly_chart(fig3, use_container_width=True)
    with colD:
        por_uf_lt = por_uf.copy()
        por_uf_lt["lead_time_h"] = por_uf_lt["leed_time_seg"] / 3600
        por_uf_lt = por_uf_lt.sort_values("lead_time_h", ascending=False)
        if tem_periodo_anterior:
            lt_prev_h = por_uf_lt["uf"].map(lambda u: lt_prev_por_uf.get(u))
            lt_prev_h = lt_prev_h.apply(lambda s: s / 3600 if pd.notna(s) else None)
            fig4 = go.Figure()
            fig4.add_bar(name="Período anterior", x=por_uf_lt["uf"], y=lt_prev_h, marker_color=COR_CINZA,
                         text=[f"{v:.1f}h" if pd.notna(v) else "" for v in lt_prev_h], textposition="outside")
            fig4.add_bar(name="Período atual", x=por_uf_lt["uf"], y=por_uf_lt["lead_time_h"], marker_color=COR_AZUL,
                         text=[f"{v:.1f}h" for v in por_uf_lt["lead_time_h"]], textposition="outside")
            fig4.update_layout(title="Lead Time médio por UF — atual vs anterior (horas)", height=350,
                                margin=dict(l=10, r=10, t=40, b=30), barmode="group",
                                legend=dict(orientation="h", yanchor="bottom", y=1.02), yaxis_title="Horas")
        else:
            fig4 = go.Figure(go.Bar(
                x=por_uf_lt["uf"], y=por_uf_lt["lead_time_h"], marker_color=COR_AZUL,
                text=[f"{v:.1f}h" for v in por_uf_lt["lead_time_h"]], textposition="outside",
            ))
            fig4.update_layout(title="Lead Time médio por UF (horas)", height=350, margin=dict(l=10, r=10, t=40, b=30), yaxis_title="Horas")
        st.plotly_chart(fig4, use_container_width=True)
    st.caption("Ordenado do maior pro menor em ambos — o pior caso aparece primeiro.")
    st.caption("两图均按数值从大到小排序 —— 最差的情况排在最前面。")

    st.divider()

    # -------------------- Comparativo Semanal --------------------
    st.header("📊 Comparativo Semanal — Sem1 vs Sem2 / 周度对比 — 第1周 vs 第2周")
    st.caption(
        "Compara as duas semanas mais recentes dentro do filtro atual (coluna `week`, "
        "calculada automaticamente pelo banco). Var. DSP / Var. Finalização: positivo = "
        "piorou (vermelho). Var. Vol: positivo = mais pacotes (verde)."
    )
    st.caption(
        "对比当前筛选条件下最近的两周（week 列，由数据库自动计算）。"
        "DSP变化量/签收变化量：正值=恶化（红色）。件量变化：正值=件量增加（绿色）。"
    )

    colunas_formato = {
        "Parado DSP (h)": "{:.1f}", "Finalização (h)": "{:.1f}",
        "Parado DSP Sem2 (h)": "{:.1f}", "Finalização Sem2 (h)": "{:.1f}",
        "Vol Sem1": lambda x: f"{x:,}".replace(",", "."),
        "Vol Sem2": lambda x: f"{x:,}".replace(",", "."),
        "Var. DSP (h)": "{:+.1f}", "Var. Final (h)": "{:+.1f}", "Var. Vol": "{:+d}",
    }

    def renderizar_comparativo(group_col, rotulo_grupo):
        tabela, sem1, sem2 = montar_comparativo_semanal(group_col, rotulo_grupo, clauses_atual, params)
        if tabela is None:
            st.warning("Não há 2 semanas distintas no filtro atual para montar o comparativo.")
            return
        st.caption(f"Semana 1: **{sem1}** · Semana 2: **{sem2}** / 第1周: **{sem1}** · 第2周: **{sem2}**")
        styler = (
            tabela.style
            .map(lambda v: cor_variacao(v, inverso=True), subset=["Var. DSP (h)", "Var. Final (h)"])
            .map(lambda v: cor_variacao(v, inverso=False), subset=["Var. Vol"])
            .format(colunas_formato)
        )
        st.dataframe(styler, use_container_width=True)
        st.plotly_chart(grafico_variacao_semanal(tabela, rotulo_grupo), use_container_width=True)

    sub_uf, sub_iata = st.tabs(["Por UF / 按州", "Por Ponto de Entrega (IATA) / 按派送网点（IATA）"])
    with sub_uf:
        renderizar_comparativo("estado_do_destinatario", "UF")
    with sub_iata:
        renderizar_comparativo("ponto_de_entrega", "IATA")

with tab_upload:
    render_upload()

with tab_logistico:
    render_logistico()

with tab_painel:
    st.subheader("Painel de pontualidade")

    sups_df = run_query("SELECT DISTINCT supervisor FROM public.supervisores ORDER BY supervisor")
    opcoes_supervisor = ["Todos"] + sups_df["supervisor"].tolist()

    periodos_df = run_query(
        "SELECT DISTINCT periodo_referencia FROM public.base "
        "WHERE periodo_referencia IS NOT NULL ORDER BY periodo_referencia DESC"
    )
    opcoes_periodo = ["Todos"] + periodos_df["periodo_referencia"].tolist()

    col1, col2 = st.columns(2)
    with col1:
        supervisor_sel = st.selectbox("Supervisor", opcoes_supervisor)
    with col2:
        periodo_sel = st.selectbox("Período", opcoes_periodo)

    where_clauses = []
    params = {}
    if supervisor_sel != "Todos":
        where_clauses.append("supervisor = %(supervisor)s")
        params["supervisor"] = supervisor_sel
    if periodo_sel != "Todos":
        where_clauses.append("periodo_referencia = %(periodo)s")
        params["periodo"] = periodo_sel
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    kpi = run_query(
        f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (
            WHERE NULLIF(dias_de_atraso,'--') IS NOT NULL
              AND NULLIF(dias_de_atraso,'--')::numeric > 0
          ) AS atrasados,
          avg(EXTRACT(EPOCH FROM leed_time) / 86400.0) AS leed_time_medio_dias,
          count(*) FILTER (WHERE supervisor IS NULL) AS sem_supervisor
        FROM public.base
        {where_sql}
        """,
        params,
    ).iloc[0]

    total = int(kpi["total"] or 0)
    atrasados = int(kpi["atrasados"] or 0)
    pct_atraso = (atrasados / total * 100) if total else 0.0
    leed_medio = kpi["leed_time_medio_dias"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total de pedidos", f"{total:,}".replace(",", "."))
    c2.metric("Atrasados", f"{atrasados:,}".replace(",", "."), delta=f"{pct_atraso:.1f}% do total",
               delta_color="inverse")
    c3.metric("Leed time médio", f"{leed_medio:.1f} dias" if leed_medio is not None else "—")
    c4.metric("Sem supervisor", int(kpi["sem_supervisor"] or 0))

    st.markdown("#### Por supervisor")
    breakdown = run_query(
        f"""
        SELECT
          COALESCE(supervisor, '(sem supervisor)') AS supervisor,
          count(*) AS pedidos,
          count(*) FILTER (
            WHERE NULLIF(dias_de_atraso,'--') IS NOT NULL
              AND NULLIF(dias_de_atraso,'--')::numeric > 0
          ) AS atrasados
        FROM public.base
        {where_sql}
        GROUP BY supervisor
        ORDER BY pedidos DESC
        """,
        params,
    )
    if len(breakdown):
        breakdown["% atraso"] = (breakdown["atrasados"] / breakdown["pedidos"] * 100).round(1)
        col_chart, col_table = st.columns([2, 3])
        with col_chart:
            st.bar_chart(breakdown.set_index("supervisor")["% atraso"], color=ANJUN_RED)
        with col_table:
            st.dataframe(breakdown, use_container_width=True, hide_index=True)

    st.markdown("#### Pedidos atrasados (detalhe)")
    atrasados_df = run_query(
        f"""
        SELECT numero_do_waybill, supervisor, ponto_de_entrega, cidade_do_destinatario,
               status_do_pacote, dias_de_atraso, criacao, finalizacao
        FROM public.base
        {where_sql}{' AND ' if where_sql else 'WHERE '}
          NULLIF(dias_de_atraso,'--') IS NOT NULL AND NULLIF(dias_de_atraso,'--')::numeric > 0
        ORDER BY NULLIF(dias_de_atraso,'--')::numeric DESC
        LIMIT 200
        """,
        params,
    )
    st.dataframe(atrasados_df, use_container_width=True, hide_index=True)

# ===================== ABA 3: SUPERVISORES =====================
with tab_supervisores:
    st.subheader("Pontos sem supervisor cadastrado")
    pendentes = run_query(
        """
        SELECT ponto_de_entrega AS ponto, count(*) AS pacotes
        FROM public.base
        WHERE supervisor IS NULL
        GROUP BY ponto_de_entrega
        ORDER BY pacotes DESC
        """
    )
    if len(pendentes):
        st.warning(f"{len(pendentes)} ponto(s) com pacotes na base mas sem supervisor.")
        st.dataframe(pendentes, use_container_width=True, hide_index=True)
    else:
        st.success("Todos os pontos com pacotes na base têm supervisor cadastrado.")

    st.markdown("#### Cadastro atual")
    sups_full = run_query(
        "SELECT ponto, supervisor, atualizado_em FROM public.supervisores ORDER BY supervisor, ponto"
    )
    st.dataframe(sups_full, use_container_width=True, hide_index=True)

    st.markdown("#### Adicionar / atualizar ponto")
    with st.form("form_supervisor", clear_on_submit=True):
        col_a, col_b = st.columns(2)
        with col_a:
            ponto_input = st.text_input("Código do ponto (ex: PA-W-D040)")
        with col_b:
            supervisor_input = st.text_input("Nome do supervisor")
        submitted = st.form_submit_button("Salvar", type="primary")
        if submitted:
            if not ponto_input or not supervisor_input:
                st.error("Preencha os dois campos.")
            else:
                ponto_clean = ponto_input.strip().upper()
                supervisor_clean = supervisor_input.strip().upper()
                upsert_supervisor(ponto_clean, supervisor_clean)
                n = recalcular_pendentes(ponto_clean)
                st.success(
                    f"{ponto_clean} -> {supervisor_clean} salvo. "
                    f"{n} linha(s) já existentes na base foram atualizadas."
                )
                st.cache_data.clear()
                st.rerun()