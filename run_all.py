#!/usr/bin/env python3
"""
Lanceur unifié pour hébergement Cloud 24/7 (Render, Koyeb, Railway, etc.)
Exécute simultanément le Bot Telegram et le Serveur Web sur un seul conteneur.
"""

import os
import sys
import subprocess
import time
import signal

def main():
    print("🚀 Démarrage du système unifié Terminat-AI (Bot + Web)...")
    
    python_exe = sys.executable
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    
    # 1. Démarrer le Bot Telegram
    print("⚽ Lancement du Bot Telegram & Collecteur...")
    bot_proc = subprocess.Popen([python_exe, "-u", "telegram_bot.py"], env=env)
    
    # Petit délai pour laisser le bot initialiser la base si besoin
    time.sleep(2)
    
    # 2. Démarrer le Serveur Web
    port = os.environ.get("PORT", "8080")
    print(f"🌐 Lancement du Serveur Web sur le port {port}...")
    web_proc = subprocess.Popen([python_exe, "-u", "web_server.py"], env=env)
    
    def handle_exit(signum, frame):
        print("\n🛑 Arrêt des services...")
        try:
            bot_proc.terminate()
            web_proc.terminate()
        except Exception:
            pass
        sys.exit(0)
        
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)
    
    # Surveillance des deux processus
    while True:
        # Si le bot plante, on le relance automatiquement
        if bot_proc.poll() is not None:
            print("⚠️ Le Bot Telegram s'est arrêté. Redémarrage automatique...")
            bot_proc = subprocess.Popen([python_exe, "-u", "telegram_bot.py"], env=env)
            
        # Si le web plante, on le relance automatiquement
        if web_proc.poll() is not None:
            print("⚠️ Le Serveur Web s'est arrêté. Redémarrage automatique...")
            web_proc = subprocess.Popen([python_exe, "-u", "web_server.py"], env=env)
            
        time.sleep(5)

if __name__ == "__main__":
    main()
