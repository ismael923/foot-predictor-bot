import telegram_bot  # noqa: F401
from my_ai_brain import TerminatAI
from telegram_bot import MarketQuote

brain = TerminatAI()
q_no = MarketQuote("extra-penalties:1094:2211:Non", "Non", 1094, 2211, 2.536,
                   group_label="Pénalités", outcome_label="Non",
                   is_extra_penalties=True)
q_yes = MarketQuote("extra-penalties:1094:2210:Oui", "Oui", 1094, 2210, 5.12,
                    group_label="Pénalités", outcome_label="Oui",
                    is_extra_penalties=True)

sib = [q_yes.odds, q_no.odds]
p_no = brain._calibrated_regulation_probability(q_no, 6, "ÉQUILIBRÉ", False, sib)
p_yes = brain._calibrated_regulation_probability(q_yes, 6, "ÉQUILIBRÉ", False, sib)
m_no = brain._bookmaker_fair_probability(q_no, sib)
print(f"marche devigore Non  = {m_no*100:.1f}%  (value={m_no*2.536:.2f})")
print(f"calibree Non/Apres  = {p_no*100:.1f}%  (value={p_no*2.536:.2f})")
print(f"calibree Oui        = {p_yes*100:.1f}%")
print(f"somme               = {100*(p_no+p_yes):.1f}%   (doit rester <= 100)")

e = brain.market_profitability("extra-penalties:1094:2211:Non")
print(f"\nverdict={e['verdict']} ROI={e['roi_pct']:+.1f}% "
      f"taux observe={e['actual_rate']*100:.1f}% n={e['bets']}")
print(f"valeur max attendue ~ {e['actual_rate']*2.536:.2f}")
