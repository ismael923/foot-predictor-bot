import sqlite3

con = sqlite3.connect('penalty_history.db')
c = con.cursor()

# Supprimer les anciens doublons non standard s'il y a conflit de clé
mappings = [
    (14, 183, 'btts:183:Oui'),
    (14, 182, 'btts:182:Non'),
    (2270, 2512, 'even-odd:2512:Total impair'),
    (2270, 2511, 'even-odd:2511:Total pair'),
    (776, 1422, 'team-pens:776:1422'),
    (776, 1423, 'team-pens:776:1423'),
    (776, 1424, 'team-pens:776:1424'),
    (776, 1425, 'team-pens:776:1425'),
]

for gid, et, new_key in mappings:
    # Trouver les enregistrements existant avec la nouvelle clé pour éviter le conflit
    c.execute("""
        DELETE FROM regulation_markets_history 
        WHERE group_id = ? AND event_type = ? AND market_key != ?
        AND game_id IN (
            SELECT game_id FROM regulation_markets_history WHERE market_key = ?
        )
    """, (gid, et, new_key, new_key))
    
    # Mettre à jour le reste
    c.execute("""
        UPDATE regulation_markets_history 
        SET market_key = ? 
        WHERE group_id = ? AND event_type = ?
    """, (new_key, gid, et))

con.commit()
print("Synchronisation réussie.")
con.close()
