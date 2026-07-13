import streamlit as st

st.set_page_config(
    page_title="Reental — Herramienta interna",
    page_icon="🏠",
    layout="wide",
)

pg = st.navigation([
    st.Page("pages/Analizador_de_Wallets.py", title="Analizador de Wallets",   icon="🏠"),
    st.Page("pages/01_Simulador.py",          title="Simulador de carteras",   icon="🏗️"),
    st.Page("pages/02_OTC.py",                title="OTC interno Reental",     icon="🏢"),
    st.Page("pages/03_Analisis_P2P.py",       title="Análisis Oportunidades P2P", icon="📊"),
    st.Page("pages/04_Aave_Mercado.py",        title="Mercado Aave",           icon="🏦"),
])
pg.run()
