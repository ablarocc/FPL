import streamlit as st
import pandas as pd
import plotly.express as px
import glob
import os

# Configuración de la página
st.set_page_config(
    page_title="FPL Draft Dashboard & Comparador",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Estilos visuales
st.markdown("""
<style>
    .metric-card {
        background-color: #1e2130;
        border-radius: 10px;
        padding: 15px;
        border: 1px solid #2e3450;
    }
</style>
""", unsafe_allow_html=True)

st.title("⚽ Premier League Draft Dashboard & Player Comparison")
st.caption("Herramienta interactiva para analizar, comparar y elegir jugadores en tu Draft de la Premier League.")

# Carga de datos con caché
@st.cache_data
def cargar_datos():
    # Buscar todos los CSV dentro de 'datos/' y sus subcarpetas
    archivos_csv = glob.glob("datos/**/*.csv", recursive=True) + glob.glob("datos/*.csv")
    archivos_jugadores = [f for f in archivos_csv if "clean" in f.lower() or "player" in f.lower() or "merged" in f.lower()]
    archivos_a_cargar = archivos_jugadores if archivos_jugadores else archivos_csv
    
    if not archivos_a_cargar:
        return pd.DataFrame()

    lista_df = []
    for ruta in archivos_a_cargar:
        try:
            temp_df = pd.read_csv(ruta, low_memory=False)
            
            # Extraer la temporada a partir del nombre de la carpeta (ej. 2024-2025)
            partes = ruta.split(os.sep)
            temporada = "Desconocida"
            for parte in partes:
                if "-" in parte and any(char.isdigit() for char in parte):
                    temporada = parte
                    break
            temp_df['Temporada'] = temporada
            lista_df.append(temp_df)
        except Exception:
            continue

    if not lista_df:
        return pd.DataFrame()

    df = pd.concat(lista_df, ignore_index=True)

    # Mapeo y estandarización de columnas
    renombres = {
        'first_name': 'Nombre',
        'second_name': 'Apellido',
        'web_name': 'Jugador',
        'element_type': 'Posicion',
        'team': 'Equipo',
        'goals_scored': 'Goles',
        'assists': 'Asistencias',
        'total_points': 'Puntos Totales',
        'minutes': 'Minutos',
        'yellow_cards': 'T. Amarillas',
        'red_cards': 'T. Rojas',
        'now_cost': 'Precio (x10)',
        'ict_index': 'Índice ICT',
        'influence': 'Influencia',
        'creativity': 'Creatividad',
        'threat': 'Amenaza'
    }
    
    df = df.rename(columns={k: v for k, v in renombres.items() if k in df.columns})
    
    # Mapear números a códigos de posición si es necesario
    pos_map = {1: 'GKP', 2: 'DEF', 3: 'MID', 4: 'FWD'}
    if 'Posicion' in df.columns and df['Posicion'].dtype in ['int64', 'float64']:
        df['Posicion'] = df['Posicion'].map(pos_map)
        
    if 'Jugador' not in df.columns and 'Nombre' in df.columns and 'Apellido' in df.columns:
        df['Jugador'] = df['Nombre'] + " " + df['Apellido']

    return df

# --- EJECUCIÓN DE CARGA DE DATOS ---
df_raw = cargar_datos()

if df_raw.empty:
    st.warning("⚠️ No se encontraron archivos CSV en la carpeta `datos/` ni en sus subcarpetas.")
    st.stop()

# --- BARRA LATERAL (FILTROS) ---
st.sidebar.header("🔍 Filtros del Draft")

# Filtro de Temporada
temporadas_disponibles = sorted(df_raw['Temporada'].dropna().unique().tolist()) if 'Temporada' in df_raw.columns else []
temp_seleccionada = st.sidebar.multiselect("Temporada", options=temporadas_disponibles, default=temporadas_disponibles)

# Filtro de Posición
posiciones_disponibles = sorted(df_raw['Posicion'].dropna().unique().tolist()) if 'Posicion' in df_raw.columns else []
pos_seleccionadas = st.sidebar.multiselect("Posición", options=posiciones_disponibles, default=posiciones_disponibles)

# Aplicar filtros
df = df_raw.copy()
if temp_seleccionada and 'Temporada' in df.columns:
    df = df[df['Temporada'].isin(temp_seleccionada)]

if pos_seleccionadas and 'Posicion' in df.columns:
    df = df[df['Posicion'].isin(pos_seleccionadas)]

# Filtro de Minutos
max_minutos = int(df['Minutos'].max()) if ('Minutos' in df.columns and not df.empty and pd.notna(df['Minutos'].max())) else 3800
minutos_filtro = st.sidebar.slider("Minutos jugados (mínimo)", min_value=0, max_value=max_minutos, value=100)

if 'Minutos' in df.columns and not df.empty:
    df = df[df['Minutos'] >= minutos_filtro]

# --- PESTAÑAS PRINCIPALES ---
tab1, tab2, tab3 = st.tabs(["📊 Visión General & Recomendaciones", "⚔️ Comparador Cara a Cara", "🃏 Tarjetas & Disciplina"])

# === TAB 1: VISIÓN GENERAL & RECOMENDACIONES ===
with tab1:
    st.subheader("💡 Recomendaciones Top Pick para el Draft")
    
    if not df.empty:
        col1, col2, col3 = st.columns(3)
        
        with col1:
            if 'Goles' in df.columns and not df['Goles'].isna().all():
                top_goles = df.sort_values(by='Goles', ascending=False).iloc[0]
                st.metric("🔥 Máximo Goleador", f"{top_goles['Jugador']}", f"{int(top_goles['Goles'])} goles")
                
        with col2:
            if 'Asistencias' in df.columns and not df['Asistencias'].isna().all():
                top_asist = df.sort_values(by='Asistencias', ascending=False).iloc[0]
                st.metric("🎯 Máximo Asistente", f"{top_asist['Jugador']}", f"{int(top_asist['Asistencias'])} asist.")
                
        with col3:
            if 'Puntos Totales' in df.columns and not df['Puntos Totales'].isna().all():
                top_pts = df.sort_values(by='Puntos Totales', ascending=False).iloc[0]
                st.metric("⭐ Mayor Puntaje FPL", f"{top_pts['Jugador']}", f"{int(top_pts['Puntos Totales'])} pts")

        st.markdown("---")
        st.subheader("📈 Rendimiento: Goles vs. Asistencias")
        
        if set(['Jugador', 'Goles', 'Asistencias']).issubset(df.columns):
            fig_scatter = px.scatter(
                df, 
                x='Asistencias', 
                y='Goles', 
                size='Puntos Totales' if 'Puntos Totales' in df.columns else None,
                color='Posicion' if 'Posicion' in df.columns else None,
                hover_name='Jugador',
                title="Relación Goles vs Asistencias (Tamaño de burbuja = Puntos Totales)",
                template="plotly_dark"
            )
            st.plotly_chart(fig_scatter, use_container_width=True)

        st.subheader("📋 Tabla General de Jugadores")
        columnas_visibles = [c for c in ['Jugador', 'Posicion', 'Temporada', 'Goles', 'Asistencias', 'Puntos Totales', 'Minutos', 'T. Amarillas', 'T. Rojas'] if c in df.columns]
        st.dataframe(df[columnas_visibles].sort_values(by=columnas_visibles[3] if len(columnas_visibles)>3 else columnas_visibles[0], ascending=False), use_container_width=True)
    else:
        st.info("No hay datos disponibles para los filtros seleccionados.")

# === TAB 2: COMPARADOR CARA A CARA ===
with tab2:
    st.subheader("⚔️ Comparar Jugadores Cara a Cara")
    
    if not df.empty:
        lista_jugadores = sorted(df['Jugador'].dropna().unique().tolist())
        jugadores_sel = st.multiselect("Selecciona jugadores para comparar:", options=lista_jugadores, default=lista_jugadores[:2] if len(lista_jugadores)>=2 else [])
        
        if len(jugadores_sel) >= 2:
            df_comp = df[df['Jugador'].isin(jugadores_sel)]
            metricas = [m for m in ['Goles', 'Asistencias', 'Puntos Totales', 'Minutos'] if m in df_comp.columns]
            
            fig_bar = px.bar(
                df_comp, 
                x='Jugador', 
                y=metricas, 
                barmode='group',
                color='Temporada' if 'Temporada' in df_comp.columns else None,
                title="Comparativa Directa de Estadísticas Principales",
                template="plotly_dark"
            )
            st.plotly_chart(fig_bar, use_container_width=True)
            
            st.markdown("### Resumen Tabular")
            st.dataframe(df_comp[columnas_visibles], use_container_width=True)
        else:
            st.info("Selecciona al menos dos jugadores para habilitar la comparación.")

# === TAB 3: TARJETAS Y DISCIPLINA ===
with tab3:
    st.subheader("🟨 ROJAS Y AMARILLAS: Análisis de Disciplina")
    st.caption("Puntos negativos o riesgo de sanción para tu selección de Draft.")
    
    if not df.empty and set(['T. Amarillas', 'T. Rojas']).issubset(df.columns):
        fig_cards = px.bar(
            df.sort_values(by='T. Amarillas', ascending=False).head(15),
            x='Jugador',
            y=['T. Amarillas', 'T. Rojas'],
            title="Top 15 Jugadores con Mayor Cantidad de Tarjetas",
            color_discrete_map={'T. Amarillas': '#f1c40f', 'T. Rojas': '#e74c3c'},
            template="plotly_dark"
        )
        st.plotly_chart(fig_cards, use_container_width=True)
