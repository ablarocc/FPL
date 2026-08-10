import dash
from dash import dcc, html, dash_table, Input, Output
import pandas as pd
import plotly.express as px
import glob
import os

# --- 1. CARGA Y PROCESAMIENTO DE DATOS ---
def load_data():
    # Buscar todos los CSVs dentro de data/ o datos/
    csv_files = (
        glob.glob("data/**/*.csv", recursive=True) + 
        glob.glob("data/*.csv") + 
        glob.glob("datos/**/*.csv", recursive=True) + 
        glob.glob("datos/*.csv")
    )
    
    # Filtrar únicamente los archivos de jugadores principales
    target_files = [f for f in csv_files if any(name in f.lower() for name in ["players.csv", "playerstats.csv"])]
    files_to_load = target_files if target_files else csv_files
    
    if not files_to_load:
        return pd.DataFrame()

    df_list = []
    for path in files_to_load:
        try:
            temp_df = pd.read_csv(path, low_memory=False)
            
            # Identificar temporada desde la ruta de carpetas (ej. 2024-2025)
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

    # Mapeo universal de columnas de FPL
    column_mapping = {
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
    
    col_map = {col: column_mapping[col.lower().strip()] for col in df.columns if col.lower().strip() in column_mapping}
    df = df.rename(columns=col_map)
    
    # Decodificar posiciones numéricas
    pos_map = {1: 'GKP', 2: 'DEF', 3: 'MID', 4: 'FWD'}
    if 'Position' in df.columns and df['Position'].dtype in ['int64', 'float64']:
        df['Position'] = df['Position'].map(pos_map)
        
    if 'Player' not in df.columns and 'First Name' in df.columns and 'Second Name' in df.columns:
        df['Player'] = df['First Name'].astype(str) + " " + df['Second Name'].astype(str)

    return df

df_data = load_data()

# --- 2. INICIALIZACIÓN DE LA APLICACIÓN DASH ---
app = dash.Dash(
    __name__,
    external_stylesheets=['https://codepen.io/chriddyp/pen/bWLwgP.css'],
    title="FPL Draft Dashboard"
)
server = app.server  # Variable del servidor requerida para el hosting

# Extraer opciones únicas para filtros
available_seasons = sorted(df_data['Season'].dropna().unique().tolist()) if 'Season' in df_data.columns else []
available_positions = sorted(df_data['Position'].dropna().unique().tolist()) if 'Position' in df_data.columns else []

# --- 3. ESTRUCTURA DE LA INTERFAZ (LAYOUT) ---
app.layout = html.Div([
    
    # Encabezado principal
    html.Div([
        html.H1("⚽ Premier League Draft Dashboard", style={'textAlign': 'center', 'color': '#0F172A', 'marginTop': '20px'}),
        html.P("Interactive FPL Player Performance & Selection Tool", style={'textAlign': 'center', 'color': '#64748B'}),
    ]),

    # Sección de Filtros
    html.Div([
        html.Div([
            html.Label("Season:", style={'fontWeight': 'bold'}),
            dcc.Dropdown(
                id='season-filter',
                options=[{'label': s, 'value': s} for s in available_seasons],
                value=available_seasons,
                multi=True,
                placeholder="Select Season"
            )
        ], className="six columns"),

        html.Div([
            html.Label("Position:", style={'fontWeight': 'bold'}),
            dcc.Dropdown(
                id='position-filter',
                options=[{'label': p, 'value': p} for p in available_positions],
                value=available_positions,
                multi=True,
                placeholder="Select Position"
            )
        ], className="six columns"),
    ], className="row", style={'width': '90%', 'margin': '20px auto'}),

    # Sección de Gráficos
    html.Div([
        html.Div([
            dcc.Graph(id='scatter-goals-assists')
        ], className="six columns"),

        html.Div([
            dcc.Graph(id='bar-top-points')
        ], className="six columns"),
    ], className="row", style={'width': '90%', 'margin': '0 auto'}),

    # Tabla Interactiva
    html.Div([
        html.H3("📋 Player Statistics Table", style={'marginTop': '30px', 'color': '#1E293B'}),
        dash_table.DataTable(
            id='player-table',
            columns=[{"name": col, "id": col} for col in ['Player', 'Position', 'Season', 'Goals', 'Assists', 'Total Points', 'Minutes'] if col in df_data.columns],
            data=df_data.to_dict('records') if not df_data.empty else [],
            page_size=15,
            sort_action="native",
            filter_action="native",
            style_table={'overflowX': 'auto'},
            style_header={'backgroundColor': '#1E293B', 'color': 'white', 'fontWeight': 'bold'},
            style_cell={'textAlign': 'left', 'padding': '10px', 'fontFamily': 'sans-serif'}
        )
    ], style={'width': '90%', 'margin': '20px auto'})

], style={'backgroundColor': '#F8FAFC', 'padding': '10px 20px', 'fontFamily': 'sans-serif'})

# --- 4. LÓGICA DE ACTUALIZACIÓN (CALLBACKS) ---
@app.callback(
    [Output('scatter-goals-assists', 'figure'),
     Output('bar-top-points', 'figure'),
     Output('player-table', 'data')],
    [Input('season-filter', 'value'),
     Input('position-filter', 'value')]
)
def update_dashboard(selected_seasons, selected_positions):
    filtered_df = df_data.copy()

    if filtered_df.empty:
        empty_fig = px.scatter(title="No data found")
        return empty_fig, empty_fig, []

    # Filtrado por Temporada
    if selected_seasons and 'Season' in filtered_df.columns:
        filtered_df = filtered_df[filtered_df['Season'].isin(selected_seasons)]

    # Filtrado por Posición
    if selected_positions and 'Position' in filtered_df.columns:
        filtered_df = filtered_df[filtered_df['Position'].isin(selected_positions)]

    # 1. Gráfico: Goles vs Asistencias
    fig_scatter = px.scatter(
        filtered_df,
        x='Assists' if 'Assists' in filtered_df.columns else None,
        y='Goals' if 'Goals' in filtered_df.columns else None,
        color='Position' if 'Position' in filtered_df.columns else None,
        hover_name='Player' if 'Player' in filtered_df.columns else None,
        title="Goals vs. Assists",
        template="plotly_white"
    )

    # 2. Gráfico: Top 10 Puntos Totales
    if 'Total Points' in filtered_df.columns and 'Player' in filtered_df.columns:
        top_10 = filtered_df.sort_values(by='Total Points', ascending=False).head(10)
        fig_bar = px.bar(
            top_10,
            x='Player',
            y='Total Points',
            color='Position' if 'Position' in top_10.columns else None,
            title="Top 10 Players by Total Points",
            template="plotly_white"
        )
    else:
        fig_bar = px.bar(title="Top 10 Players")

    return fig_scatter, fig_bar, filtered_df.to_dict('records')

if __name__ == '__main__':
    app.run_server(debug=True)
