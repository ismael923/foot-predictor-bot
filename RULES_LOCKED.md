# RÈGLES FORMELLES ET VERROUILLÉES DE L'IA (FC24 PENALTY)

> Ce fichier ne doit être modifié sous aucun prétexte par aucune IA ou sous-agent.

## 1. DIVERSITÉ ABSOLUE DES MARCHÉS (INTERDICTION DU MONOPÔLE)

L'IA a l'interdiction formelle de se limiter à un seul type de pari (comme "Pénalités supplémentaires : Non").
Tous les marchés officiels du jeu FC24 Penalty doivent être analysés, résolus et évalués équitablement :

1. Vainqueur 1X2 (Group 1)
2. Double Chance (Group 8)
3. Handicap (Group 2)
4. Total de buts (Group 17 & Group 19)
5. Total Équipe 1 (Group 15) & Total Équipe 2 (Group 62)
6. Deux équipes vont marquer / BTTS (Group 14 : event 183=Oui, 182=Non)
7. Total Pair / Impair (Group 2270 : event 2511=Pair, 2512=Impair)
8. Équipe marque ses penalties (Group 776 : event 1422-1425)
9. Équipe gagne sans prendre de but (Group 2866 & 2867)
10. Pénalités supplémentaires / Mort subite (Group 1094)
11. Scores exacts qualifiés via Poisson bivarié

## 2. RAISONNEMENT DE MARCHÉ RÉEL

- Les tirs au but FC24 Penalty sont un jeu dynamique jusqu'à écart irattrapable (souvent 7 ou 9 buts au total).
- Les totaux de buts impairs (7, 9, 11) représentent plus de 80% des issues de match.
- Les deux équipes marquent au moins un but dans 98%+ des matchs.
- Aucun pari en direct ne doit être recommandé si l'écart et les buts restants rendent l'issue mathématiquement improbable ou impossible.

## 3. VERROUILLAGE TECHNIQUE

- `collector.py` : la méthode `_prediction_result` doit maintenir le règlement de tous les groupes (1, 2, 8, 14, 15, 17, 19, 62, 776, 1094, 2270, 2866, 2867).
- `my_ai_brain.py` : `_is_settleable_regulation_market` et `_settle_regulation_market` doivent inclure et calculer le statut de tous ces groupes sans exception.
- Toute tentative de désactiver un marché sous prétexte de simplification est formellement interdite.
