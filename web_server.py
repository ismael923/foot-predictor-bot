#!/usr/bin/env python3
"""
Serveur Web Dashboard en Direct pour Terminat-AI (FC24 Penalty)
Fournit une interface web moderne, responsive et temps réel pour suivre les pronostics en direct.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import os
from pathlib import Path
from typing import Any

from aiohttp import web

DB_PATH = Path(__file__).with_name("penalty_history.db")
PORT = int(os.environ.get("PORT", 8080))


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def calculate_metrics() -> dict[str, Any]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN prediction_status = 'WON' THEN 1 ELSE 0 END) as won,
                SUM(CASE WHEN prediction_status = 'LOST' THEN 1 ELSE 0 END) as lost,
                SUM(CASE WHEN prediction_status = 'PENDING' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN prediction_status = 'WON' THEN (odds - 1.0) WHEN prediction_status = 'LOST' THEN -1.0 ELSE 0 END) as net_profit,
                AVG(CASE WHEN odds IS NOT NULL THEN odds ELSE NULL END) as avg_odds
            FROM sent_predictions
            WHERE delivery_status = 'SENT'
        """)
        row = cursor.fetchone()
        
        total = row["total"] or 0
        won = row["won"] or 0
        lost = row["lost"] or 0
        pending = row["pending"] or 0
        net_profit = round(row["net_profit"] or 0.0, 2)
        avg_odds = round(row["avg_odds"] or 0.0, 2)
        
        decided = won + lost
        winrate = round((won / decided * 100), 1) if decided > 0 else 0.0
        roi = round((net_profit / decided * 100), 1) if decided > 0 else 0.0
        
        return {
            "total": total,
            "won": won,
            "lost": lost,
            "pending": pending,
            "winrate": winrate,
            "roi": roi,
            "net_profit": net_profit,
            "avg_odds": avg_odds,
        }


def get_latest_prediction() -> dict[str, Any] | None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                sp.id, sp.game_id, sp.phase, sp.market_label, sp.odds, sp.confidence_pct, 
                sp.prediction_status, sp.final_score1, sp.final_score2, sp.sent_at,
                mh.match_number, mh.team1, mh.team2, mh.status as match_status,
                mh.final_score1 as m_score1, mh.final_score2 as m_score2
            FROM sent_predictions sp
            LEFT JOIN matches_history mh ON sp.game_id = mh.game_id
            WHERE sp.delivery_status = 'SENT'
            ORDER BY sp.id DESC LIMIT 1
        """)
        row = cursor.fetchone()
        if not row:
            return None
        
        d = dict(row)
        d["confidence_pct"] = round(d["confidence_pct"] or 0, 1)
        d["odds"] = round(d["odds"] or 0, 3)
        return d


def get_predictions_history(limit: int = 40) -> list[dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                sp.id, sp.game_id, sp.phase, sp.market_label, sp.odds, sp.confidence_pct, 
                sp.prediction_status, sp.final_score1, sp.final_score2, sp.sent_at,
                mh.match_number, mh.team1, mh.team2, mh.status as match_status,
                mh.final_score1 as m_score1, mh.final_score2 as m_score2
            FROM sent_predictions sp
            LEFT JOIN matches_history mh ON sp.game_id = mh.game_id
            WHERE sp.delivery_status = 'SENT'
            ORDER BY sp.id DESC LIMIT ?
        """, (limit,))
        
        result = []
        for row in cursor.fetchall():
            d = dict(row)
            d["confidence_pct"] = round(d["confidence_pct"] or 0, 1)
            d["odds"] = round(d["odds"] or 0, 3)
            result.append(d)
        return result


async def api_stats(request: web.Request) -> web.Response:
    metrics = calculate_metrics()
    latest = get_latest_prediction()
    return web.json_response({
        "metrics": metrics,
        "latest": latest,
    })


async def api_history(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", 50))
    history = get_predictions_history(limit)
    return web.json_response(history)


INDEX_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Terminat-AI · Dashboard FC24 Penalty</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-base: #080b11;
            --bg-card: rgba(18, 24, 38, 0.75);
            --bg-card-hover: rgba(26, 35, 54, 0.85);
            --border: rgba(255, 255, 255, 0.08);
            --border-glow: rgba(99, 102, 241, 0.3);
            --text-main: #f3f4f6;
            --text-muted: #94a3b8;
            --primary: #6366f1;
            --primary-glow: rgba(99, 102, 241, 0.4);
            --accent-green: #10b981;
            --accent-green-glow: rgba(16, 185, 129, 0.35);
            --accent-red: #ef4444;
            --accent-yellow: #f59e0b;
            --neon-blue: #06b6d4;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Space Grotesk', -apple-system, sans-serif;
            background-color: var(--bg-base);
            background-image: 
                radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.12) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(16, 185, 129, 0.08) 0px, transparent 50%),
                linear-gradient(rgba(255, 255, 255, 0.02) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255, 255, 255, 0.02) 1px, transparent 1px);
            background-size: 100% 100%, 100% 100%, 30px 30px, 30px 30px;
            color: var(--text-main);
            min-height: 100vh;
            padding: 24px;
            display: flex;
            flex-direction: column;
            gap: 24px;
        }

        /* HEADER */
        header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 16px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border);
        }

        .brand {
            display: flex;
            align-items: center;
            gap: 14px;
        }

        .brand-logo {
            width: 48px;
            height: 48px;
            background: linear-gradient(135deg, #6366f1, #06b6d4);
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 24px;
            box-shadow: 0 0 20px var(--primary-glow);
        }

        .brand-info h1 {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.5px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .badge-live {
            background: rgba(16, 185, 129, 0.15);
            color: var(--accent-green);
            border: 1px solid var(--accent-green);
            padding: 2px 8px;
            border-radius: 20px;
            font-size: 0.72rem;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }

        .pulse-dot {
            width: 7px;
            height: 7px;
            background-color: var(--accent-green);
            border-radius: 50%;
            animation: pulse 1.5s infinite;
        }

        @keyframes pulse {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { transform: scale(1.1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }

        .header-actions {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .btn-sound {
            background: var(--bg-card);
            border: 1px solid var(--border);
            color: var(--text-main);
            padding: 8px 14px;
            border-radius: 8px;
            font-size: 0.85rem;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: all 0.2s;
        }
        .btn-sound:hover {
            border-color: var(--primary);
            background: var(--bg-card-hover);
        }

        /* METRICS GRID */
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
        }

        .metric-card {
            background: var(--bg-card);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 18px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            position: relative;
            overflow: hidden;
            transition: transform 0.2s, border-color 0.2s;
        }

        .metric-card:hover {
            transform: translateY(-2px);
            border-color: var(--border-glow);
        }

        .metric-title {
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
        }

        .metric-value {
            font-size: 1.8rem;
            font-weight: 700;
            font-family: 'JetBrains Mono', monospace;
        }

        .val-green { color: var(--accent-green); text-shadow: 0 0 12px var(--accent-green-glow); }
        .val-purple { color: #a78bfa; }
        .val-blue { color: var(--neon-blue); }

        /* FEATURED LIVE CARD */
        .featured-section {
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .featured-card {
            background: linear-gradient(135deg, rgba(26, 35, 60, 0.9), rgba(15, 20, 32, 0.95));
            backdrop-filter: blur(16px);
            border: 1px solid rgba(99, 102, 241, 0.4);
            border-radius: 18px;
            padding: 24px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5), inset 0 0 20px rgba(99, 102, 241, 0.15);
            display: grid;
            grid-template-columns: 1fr auto;
            gap: 24px;
            align-items: center;
            position: relative;
        }

        @media (max-width: 768px) {
            .featured-card {
                grid-template-columns: 1fr;
            }
        }

        .match-header {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 12px;
        }

        .badge-phase {
            background: #4f46e5;
            color: #fff;
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .match-id {
            color: var(--text-muted);
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.85rem;
        }

        .match-teams {
            font-size: 1.7rem;
            font-weight: 700;
            letter-spacing: -0.5px;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .vs-tag {
            font-size: 0.9rem;
            color: var(--text-muted);
            font-weight: 400;
        }

        .prono-box {
            background: rgba(0, 0, 0, 0.4);
            border: 1px dashed rgba(255, 255, 255, 0.15);
            border-radius: 12px;
            padding: 16px;
            display: inline-flex;
            flex-direction: column;
            gap: 6px;
        }

        .prono-label-sub {
            font-size: 0.75rem;
            text-transform: uppercase;
            color: var(--text-muted);
            letter-spacing: 0.5px;
        }

        .prono-target {
            font-size: 1.35rem;
            font-weight: 700;
            color: #38bdf8;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .featured-odds-panel {
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 12px;
            text-align: right;
        }

        @media (max-width: 768px) {
            .featured-odds-panel {
                align-items: flex-start;
                text-align: left;
            }
        }

        .odds-tag {
            background: linear-gradient(135deg, #10b981, #059669);
            color: white;
            font-size: 2rem;
            font-weight: 800;
            font-family: 'JetBrains Mono', monospace;
            padding: 8px 18px;
            border-radius: 12px;
            box-shadow: 0 4px 15px var(--accent-green-glow);
        }

        .confidence-meter {
            font-size: 0.95rem;
            color: var(--text-muted);
        }

        .confidence-meter strong {
            color: var(--text-main);
        }

        /* TABLE SECTION */
        .history-section {
            background: var(--bg-card);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        .section-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .section-header h2 {
            font-size: 1.15rem;
            font-weight: 600;
        }

        .table-responsive {
            overflow-x: auto;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.9rem;
        }

        th {
            padding: 12px 14px;
            color: var(--text-muted);
            font-weight: 500;
            border-bottom: 1px solid var(--border);
            text-transform: uppercase;
            font-size: 0.75rem;
            letter-spacing: 0.5px;
        }

        td {
            padding: 14px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            vertical-align: middle;
        }

        tr:hover td {
            background: rgba(255, 255, 255, 0.02);
        }

        .badge-status {
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 700;
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }

        .status-won {
            background: rgba(16, 185, 129, 0.15);
            color: var(--accent-green);
            border: 1px solid rgba(16, 185, 129, 0.4);
        }

        .status-lost {
            background: rgba(239, 68, 68, 0.15);
            color: var(--accent-red);
            border: 1px solid rgba(239, 68, 68, 0.4);
        }

        .status-pending {
            background: rgba(245, 158, 11, 0.15);
            color: var(--accent-yellow);
            border: 1px solid rgba(245, 158, 11, 0.4);
        }

        .time-tag {
            color: var(--text-muted);
            font-size: 0.8rem;
            font-family: 'JetBrains Mono', monospace;
        }

        .odds-cell {
            font-family: 'JetBrains Mono', monospace;
            font-weight: 700;
            color: #38bdf8;
        }

        footer {
            margin-top: auto;
            text-align: center;
            padding: 20px;
            color: var(--text-muted);
            font-size: 0.8rem;
            border-top: 1px solid var(--border);
        }
    </style>
</head>
<body>

    <!-- HEADER -->
    <header>
        <div class="brand">
            <div class="brand-logo">⚽</div>
            <div class="brand-info">
                <h1>Terminat-AI <span class="badge-live"><span class="pulse-dot"></span> LIVE FEED</span></h1>
                <p style="color: var(--text-muted); font-size: 0.85rem;">Intelligence Artificielle de Prédiction · FC24 Penalty</p>
            </div>
        </div>
        <div class="header-actions">
            <button class="btn-sound" id="btnSound" onclick="toggleSound()">
                <span id="soundIcon">🔊</span> Son activé
            </button>
        </div>
    </header>

    <!-- METRICS -->
    <div class="metrics-grid">
        <div class="metric-card">
            <div class="metric-title">Taux de Réussite</div>
            <div class="metric-value val-green" id="mWinrate">--%</div>
        </div>
        <div class="metric-card">
            <div class="metric-title">Bénéfice Net</div>
            <div class="metric-value val-purple" id="mNetProfit">+0.00u</div>
        </div>
        <div class="metric-card">
            <div class="metric-title">Pronostics Validés</div>
            <div class="metric-value" id="mWonLost">0 / 0</div>
        </div>
        <div class="metric-card">
            <div class="metric-title">Cote Moyenne</div>
            <div class="metric-value val-blue" id="mAvgOdds">@0.00</div>
        </div>
    </div>

    <!-- FEATURED CARD -->
    <div class="featured-section">
        <h2 style="font-size: 1.1rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px;">Dernier Pronostic Diffusé</h2>
        <div class="featured-card" id="featuredCard">
            <div class="featured-main">
                <div class="match-header">
                    <span class="badge-phase" id="fPhase">PRE-MATCH</span>
                    <span class="match-id" id="fMatchId">#-- · ID --------</span>
                </div>
                <div class="match-teams" id="fTeams">Chargement du direct...</div>
                <div class="prono-box">
                    <div class="prono-label-sub">Pari Conseillé par Terminat-AI</div>
                    <div class="prono-target" id="fMarket">Analyse en cours...</div>
                </div>
            </div>
            <div class="featured-odds-panel">
                <div class="odds-tag" id="fOdds">@--</div>
                <div class="confidence-meter">Confiance : <strong id="fConf">--%</strong></div>
                <div id="fStatusBadge"><span class="badge-status status-pending">⏳ EN ATTENTE</span></div>
            </div>
        </div>
    </div>

    <!-- HISTORY TABLE -->
    <div class="history-section">
        <div class="section-header">
            <h2>Historique en Direct des Pronostics</h2>
            <span style="font-size: 0.8rem; color: var(--text-muted);">Actualisé automatiquement</span>
        </div>
        <div class="table-responsive">
            <table>
                <thead>
                    <tr>
                        <th>Heure</th>
                        <th>Match</th>
                        <th>Type de pari</th>
                        <th>Cote</th>
                        <th>Confiance</th>
                        <th>Score Final</th>
                        <th>Résultat</th>
                    </tr>
                </thead>
                <tbody id="historyTableBody">
                    <tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 30px;">Chargement de l'historique...</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <footer>
        Terminat-AI Engine · Modèle bayésien & algorithme de valeur prédictive · Données synchronisées 1xBet FC24 Penalty
    </footer>

    <script>
        let soundEnabled = true;
        let lastPredictionId = null;

        // Audio synthétisé pour éviter tout fichier audio externe
        function playAlertSound() {
            if (!soundEnabled) return;
            try {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.setValueAtTime(587.33, ctx.currentTime); // Re5
                osc.frequency.setValueAtTime(880, ctx.currentTime + 0.1); // La5
                gain.gain.setValueAtTime(0.15, ctx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start();
                osc.stop(ctx.currentTime + 0.35);
            } catch (e) {
                console.log("Audio non disponible", e);
            }
        }

        function toggleSound() {
            soundEnabled = !soundEnabled;
            document.getElementById('soundIcon').innerText = soundEnabled ? '🔊' : '🔇';
            document.getElementById('btnSound').innerHTML = `${soundEnabled ? '🔊' : '🔇'} Son ${soundEnabled ? 'activé' : 'coupé'}`;
        }

        async function fetchDashboard() {
            try {
                const res = await fetch('/api/stats');
                if (!res.ok) return;
                const data = await res.json();
                
                // Metrics
                const m = data.metrics;
                document.getElementById('mWinrate').innerText = m.winrate + '%';
                document.getElementById('mNetProfit').innerText = (m.net_profit >= 0 ? '+' : '') + m.net_profit + 'u';
                document.getElementById('mWonLost').innerText = `${m.won}G / ${m.lost}P`;
                document.getElementById('mAvgOdds').innerText = '@' + m.avg_odds;

                // Latest featured
                const latest = data.latest;
                if (latest) {
                    if (lastPredictionId !== null && latest.id !== lastPredictionId) {
                        playAlertSound();
                    }
                    lastPredictionId = latest.id;

                    document.getElementById('fPhase').innerText = latest.phase || 'PRE-MATCH';
                    document.getElementById('fMatchId').innerText = `#${latest.match_number || '--'} · ID ${latest.game_id}`;
                    document.getElementById('fTeams').innerHTML = `${latest.team1 || 'Équipe 1'} <span class="vs-tag">vs</span> ${latest.team2 || 'Équipe 2'}`;
                    document.getElementById('fMarket').innerText = `👉 ${latest.market_label}`;
                    document.getElementById('fOdds').innerText = '@' + latest.odds;
                    document.getElementById('fConf').innerText = latest.confidence_pct + '%';

                    let badgeHtml = '';
                    if (latest.prediction_status === 'WON') {
                        badgeHtml = '<span class="badge-status status-won">🟢 GAGNÉ</span>';
                    } else if (latest.prediction_status === 'LOST') {
                        badgeHtml = '<span class="badge-status status-lost">🔴 PERDU</span>';
                    } else {
                        badgeHtml = '<span class="badge-status status-pending">⏳ EN ATTENTE</span>';
                    }
                    document.getElementById('fStatusBadge').innerHTML = badgeHtml;
                }
            } catch (err) {
                console.error("Erreur stats:", err);
            }
        }

        async function fetchHistory() {
            try {
                const res = await fetch('/api/history?limit=30');
                if (!res.ok) return;
                const rows = await res.json();
                
                const tbody = document.getElementById('historyTableBody');
                if (!rows.length) {
                    tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 20px;">Aucun historique disponible.</td></tr>';
                    return;
                }

                tbody.innerHTML = rows.map(r => {
                    let statusBadge = '<span class="badge-status status-pending">⏳ EN COURS</span>';
                    if (r.prediction_status === 'WON') statusBadge = '<span class="badge-status status-won">🟢 GAGNÉ</span>';
                    if (r.prediction_status === 'LOST') statusBadge = '<span class="badge-status status-lost">🔴 PERDU</span>';
                    
                    const score = (r.final_score1 !== null && r.final_score2 !== null) 
                        ? `<strong>${r.final_score1} - ${r.final_score2}</strong>` 
                        : (r.m_score1 !== null && r.m_score2 !== null ? `${r.m_score1} - ${r.m_score2}` : '<span style="color:var(--text-muted)">--</span>');

                    const time = (r.sent_at || '').split(' ')[1] || r.sent_at || '--:--';

                    return `
                        <tr>
                            <td class="time-tag">${time}</td>
                            <td><strong>${r.team1 || 'Eq1'}</strong> vs <strong>${r.team2 || 'Eq2'}</strong> <span style="color:var(--text-muted);font-size:0.75rem;">(#${r.match_number || ''})</span></td>
                            <td>${r.market_label}</td>
                            <td class="odds-cell">@${r.odds}</td>
                            <td>${r.confidence_pct}%</td>
                            <td>${score}</td>
                            <td>${statusBadge}</td>
                        </tr>
                    `;
                }).join('');
            } catch (err) {
                console.error("Erreur historique:", err);
            }
        }

        // Rafraîchissement automatique toutes les 4 secondes
        fetchDashboard();
        fetchHistory();
        setInterval(() => {
            fetchDashboard();
            fetchHistory();
        }, 4000);
    </script>
</body>
</html>
"""


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/history", api_history)
    return app


def main() -> None:
    app = create_app()
    print(f"🚀 Serveur Web Terminat-AI démarré sur http://localhost:{PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
