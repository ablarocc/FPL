import streamlit as st
import pandas as pd
import plotly.express as px
import glob
import os

# Page Configuration
st.set_page_config(
    page_title="FPL Draft Dashboard & Player Comparison",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS Styling
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
st.caption("Interactive tool to analyze, compare, and pick players for your Premier League Draft team.")

# Cached Data Loading Function
@st.cache_data
def load_data():
    # Search for all CSVs inside 'data/' and 'datos/' folders, at root or subdirectories
    csv_files = (
        glob.glob("data/**/*.csv", recursive=True) + 
        glob.glob("data/*.csv") + 
        glob.glob("../data/**/*.csv", recursive=True) + 
        glob.glob("../data/*.csv") +
        glob.glob("datos/**/*.csv", recursive=True) + 
        glob.glob("datos/*.csv") + 
        glob.glob("../datos/**/*.csv", recursive=True) + 
        glob.glob("../datos/*.csv")
    )
    
    # Filter player-related data files (Spanish and English keywords)
    keywords = ["jugador", "jugadores", "estadística", "estadistica", "clean", "player", "players"]
    player_files = [f for f in csv_files if any(k in f.lower() for k in keywords)]
    
    files_to_load = player_files if player_files else csv_files
    
    if not files_to_load:
        return pd.DataFrame()

    df_list = []
    for path in files_to_load:
        try:
            temp_df = pd.read_csv(path, low_memory=False)
            
            # Extract Season from directory path (e.g. 2024-2025)
            parts = path.replace("\\", "/").split("/")
            season = "Unknown"
            for part in parts:
                if "-" in part and any(char.isdigit() for char in part):
                    season = part
                    break
            temp_df['Season'] = season
            df_list.append(temp_df)
        except Exception:
            continue

    if not df_list:
        return pd.DataFrame()

    df = pd.concat(df_list, ignore_index=True)

    # Universal Column Renaming to English
    column_mapping = {
        # Spanish inputs
        'nombre': 'First Name',
        'apellido': 'Second Name',
        'jugador': 'Player',
        'posicion': 'Position',
        'posición': 'Position',
        'equipo': 'Team',
        'goles': 'Goals',
        'asistencias': 'Assists',
        'puntos_totales': 'Total Points',
        'puntos': 'Total Points',
        'minutos': 'Minutes',
        'tarjetas_amarillas': 'Yellow Cards',
        'tarjetas_rojas': 'Red Cards',
        'amarillas': 'Yellow Cards',
        'rojas': 'Red Cards',
        'temporada': 'Season',
        # English inputs
        'first_name': 'First Name',
        'second_name': 'Second Name',
        'web_name': 'Player',
        'element_type': 'Position',
        'team': 'Team',
        'goals_scored': 'Goals',
        'assists': 'Assists',
        'total_points': 'Total Points',
        'minutes': 'Minutes',
        'yellow_cards': 'Yellow Cards',
        'red_cards': 'Red Cards'
    }
    
    col_map = {}
    for col in df.columns:
        col_lower = col.lower().strip()
        if col_lower in column_mapping:
            col_map[col] = column_mapping[col_lower]
            
    df = df.rename(columns=col_map)
    
    # Map numeric positions if present
    pos_map = {1: 'GKP', 2: 'DEF', 3: 'MID', 4: 'FWD'}
    if 'Position' in df.columns and df['Position'].dtype in ['int64', 'float64']:
        df['Position'] = df['Position'].map(pos_map)
        
    if 'Player' not in df.columns and 'First Name' in df.columns and 'Second Name' in df.columns:
        df['Player'] = df['First Name'].astype(str) + " " + df['Second Name'].astype(str)

    return df

# --- LOAD DATA ---
df_raw = load_data()

if df_raw.empty:
    st.warning("⚠️ No valid data found in the `data/` folder.")
    st.stop()

# --- SIDEBAR (FILTERS) ---
st.sidebar.header("🔍 Draft Filters")

# Season Filter
available_seasons = sorted(df_raw['Season'].dropna().unique().tolist()) if 'Season' in df_raw.columns else []
selected_seasons = st.sidebar.multiselect("Season", options=available_seasons, default=available_seasons)

# Position Filter
available_positions = sorted(df_raw['Position'].dropna().unique().tolist()) if 'Position' in df_raw.columns else []
selected_positions = st.sidebar.multiselect("Position", options=available_positions, default=available_positions)

# Apply Filters
df = df_raw.copy()
if selected_seasons and 'Season' in df.columns:
    df = df[df['Season'].isin(selected_seasons)]

if selected_positions and 'Position' in df.columns:
    df = df[df['Position'].isin(selected_positions)]

# Minutes Played Filter
max_minutes = int(df['Minutes'].max()) if ('Minutes' in df.columns and not df.empty and pd.notna(df['Minutes'].max())) else 3800
minutes_filter = st.sidebar.slider("Minimum Minutes Played", min_value=0, max_value=max_minutes, value=100)

if 'Minutes' in df.columns and not df.empty:
    df = df[df['Minutes'] >= minutes_filter]

# --- MAIN TABS ---
tab1, tab2, tab3 = st.tabs(["📊 Overview & Top Picks", "⚔️ Head-to-Head Comparison", "🃏 Cards & Discipline"])

# === TAB 1: OVERVIEW & TOP PICKS ===
with tab1:
    st.subheader("💡 Recommended Top Draft Picks")
    
    if not df.empty:
        col1, col2, col3 = st.columns(3)
        
        with col1:
            if 'Goals' in df.columns and not df['Goals'].isna().all():
                top_goals = df.sort_values(by='Goals', ascending=False).iloc[0]
                st.metric("🔥 Top Scorer", f"{top_goals.get('Player', 'N/A')}", f"{int(top_goals['Goals'])} goals")
                
        with col2:
            if 'Assists' in df.columns and not df['Assists'].isna().all():
                top_assists = df.sort_values(by='Assists', ascending=False).iloc[0]
                st.metric("🎯 Top Assister", f"{top_assists.get('Player', 'N/A')}", f"{int(top_assists['Assists'])} assists")
                
        with col3:
            if 'Total Points' in df.columns and not df['Total Points'].isna().all():
                top_pts = df.sort_values(by='Total Points', ascending=False).iloc[0]
                st.metric("⭐ Highest FPL Points", f"{top_pts.get('Player', 'N/A')}", f"{int(top_pts['Total Points'])} pts")

        st.markdown("---")
        st.subheader("📈 Performance: Goals vs. Assists")
        
        if set(['Player', 'Goals', 'Assists']).issubset(df.columns):
            fig_scatter = px.scatter(
                df, 
                x='Assists', 
                y='Goals', 
                size='Total Points' if 'Total Points' in df.columns else None,
                color='Position' if 'Position' in df.columns else None,
                hover_name='Player',
                title="Goals vs. Assists Relationship (Bubble Size = Total Points)",
                template="plotly_dark"
            )
            st.plotly_chart(fig_scatter, use_container_width=True)

        st.subheader("📋 Overall Player Statistics Table")
        visible_columns = [c for c in ['Player', 'Position', 'Season', 'Goals', 'Assists', 'Total Points', 'Minutes', 'Yellow Cards', 'Red Cards'] if c in df.columns]
        st.dataframe(df[visible_columns].sort_values(by=visible_columns[3] if len(visible_columns)>3 else visible_columns[0], ascending=False), use_container_width=True)
    else:
        st.info("No data available for the selected filters.")

# === TAB 2: HEAD-TO-HEAD COMPARISON ===
with tab2:
    st.subheader("⚔️ Head-to-Head Player Comparison")
    
    if not df.empty and 'Player' in df.columns:
        player_list = sorted(df['Player'].dropna().unique().tolist())
        selected_players = st.multiselect("Select players to compare:", options=player_list, default=player_list[:2] if len(player_list)>=2 else [])
        
        if len(selected_players) >= 2:
            df_comp = df[df['Player'].isin(selected_players)]
            metrics = [m for m in ['Goals', 'Assists', 'Total Points', 'Minutes'] if m in df_comp.columns]
            
            fig_bar = px.bar(
                df_comp, 
                x='Player', 
                y=metrics, 
                barmode='group',
                color='Season' if 'Season' in df_comp.columns else None,
                title="Direct Key Statistics Comparison",
                template="plotly_dark"
            )
            st.plotly_chart(fig_bar, use_container_width=True)
            
            st.markdown("### Summary Table")
            st.dataframe(df_comp[visible_columns], use_container_width=True)
        else:
            st.info("Please select at least two players to enable comparison.")

# === TAB 3: CARDS & DISCIPLINE ===
with tab3:
    st.subheader("🟨 🟥 Cards & Discipline Analysis")
    st.caption("Negative points or suspension risks for your draft picks.")
    
    if not df.empty and set(['Yellow Cards', 'Red Cards']).issubset(df.columns):
        fig_cards = px.bar(
            df.sort_values(by='Yellow Cards', ascending=False).head(15),
            x='Player',
            y=['Yellow Cards', 'Red Cards'],
            title="Top 15 Most Booked Players",
            color_discrete_map={'Yellow Cards': '#f1c40f', 'Red Cards': '#e74c3c'},
            template="plotly_dark"
        )
        st.plotly_chart(fig_cards, use_container_width=True)
