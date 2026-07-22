# Anjun Brasil — Monitoramento de Pontualidade

App Streamlit para o supervisor subir a base semanal (modelo cru, mesmas
86 colunas de `monitoramento_da_pontualidade_de_pedido_*.xlsx`) direto no
Supabase, e acompanhar o painel de pontualidade por supervisor.

Toda a lógica de cálculo (week, leed time, hub de conexão, supervisor)
já roda dentro do banco via trigger — este app só insere as colunas cruas.

## Estrutura

```
anjun_streamlit/
├── app.py                        # app principal (3 abas)
├── requirements.txt
├── assets/anjun_logo.png         # logo usada no cabeçalho
├── config/column_mapping_base.json  # de-para header original -> coluna no banco
└── .streamlit/
    ├── config.toml                # tema (cores da Anjun)
    └── secrets.toml.example       # copie para secrets.toml e preencha
```

## Como rodar localmente

1. Instale as dependências:
   ```bash
   pip install -r requirements.txt
   ```

2. Configure a conexão com o banco:
   ```bash
   cp .streamlit/secrets.toml.example .streamlit/secrets.toml
   ```
   Abra `.streamlit/secrets.toml` e preencha `password` com a senha do
   projeto Supabase (Project Settings → Database → Connection parameters
   → aba "Session pooler"). **Não suba esse arquivo pro Git.**

3. Rode o app:
   ```bash
   streamlit run app.py
   ```

## Deploy (Streamlit Community Cloud)

1. Suba esta pasta pra um repositório no GitHub (adicione `.streamlit/secrets.toml`
   no `.gitignore` — só o `secrets.toml.example` deve ir pro repo).
2. Em [share.streamlit.io](https://share.streamlit.io), aponte para o repo e
   para `app.py`.
3. Em **App settings → Secrets**, cole o conteúdo do seu `secrets.toml`
   preenchido (o Streamlit Cloud guarda isso de forma segura, fora do repo).

## Se o modelo do arquivo mudar

Se o sistema de origem mudar as colunas do export de novo (como já
aconteceu uma vez), a aba de Upload vai barrar o arquivo e mostrar
exatamente quais colunas estão faltando ou sobrando. Quando isso
acontecer, é preciso atualizar `config/column_mapping_base.json` e o
schema da tabela `base` no Supabase juntos — não só um dos dois.