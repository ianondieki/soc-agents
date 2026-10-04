"""The triage agent: what the complaint is about, how urgent, how the customer feels, what is
risky about it, and who should handle it (docs/SUPPORT_DESK.md "Flow", "Categories").

**Weighted phrase rules, not a model.** Each category has a lexicon of phrases with weights,
written in the English, Kiswahili and Sheng that complaints actually arrive in. A category's
score is the sum of the weights of its phrases found in the normalised text; overlapping
phrases both count ("no network" and "network"), which is intended -- a specific phrase is
stronger evidence than the bare word. A few phrases carry a *negative* weight: contrast
("niko na bundle lakini siwezi browse" -- the customer has a bundle, the complaint is the
network) and homonyms ("personal data" is not a data bundle). Phrases tolerate a filler word
or two between their words (``text.FILLERS``: "cant even call") and the text has common
misspellings folded first (``text.VARIANTS``: "netwrk"). The rules are data in this module, so a reviewer can
read exactly why a complaint was called *mpesa*, and the step trace lists the phrases that
fired.

**Confidence** is ``top / (top + runner_up + 1)``: one strong phrase alone gives about 0.75,
two agreeing phrases 0.85, and two categories scoring alike drop it towards 0.4 -- below the
policy's ``low_confidence_threshold`` (0.55), which sends the case to a person rather than
letting the desk guess. The ``+ 1`` is a prior: no evidence at all is confidence 0. It is
"calibrated-ish" by construction, not fitted; the golden set's triage accuracy is the check.

**Risk flags** (fraud or SIM swap, legal or regulator, threats or safety) are separate
lexicons and any hit routes to a person, whatever the category. Three refinements keep them
honest on real phrasing: a *denied* risk word ("I don't think this is fraud, I typed the number
wrong") is masked before anything reads it; a profession or institution ("lawyer", "court") is a
legal flag only beside a cue that action is meant ("my lawyer will contact you", not "I paid my
lawyer via paybill"); and a few regexes read what a fraud complaint *describes* ("mse flani
ameingia M-PESA yangu") rather than the words it happens to use. "CA" and "CAK" are matched
case-sensitively on the original text: the Communications Authority is written in capitals,
and lower-case "ca" is noise. Note what is *not* a legal flag: the word "regulator". A
customer asking how to escalate to the regulator is asking a question the knowledge base
answers (``KB-REGULATOR-COMPLAINT``); one who names the Communications Authority, a lawyer
or a court is raising a matter a senior person must answer.

**Route**: ``human`` when any risk flag fired; ``action`` when an intent the action agent
can act on was recognised (a reversal, a bundle re-credit, a refund, an outage in a named
place, device settings); otherwise ``resolver``. The remaining escalation rules (repeat,
angry high-value, low confidence, grounding, tool limits) are applied afterwards by
:mod:`escalation`, which sees what the other agents found.

**The LLM tie-break.** With ``LLM_ENABLED`` and a port the caller is allowed to use,
a confidence below ``llm_tiebreak_below`` with at least two scoring categories is put to the
model as a choice between the top two -- nothing else. A reply naming any other category,
or any failure, leaves the rule result unchanged. An accepted tie-break lifts confidence only
to the low-confidence threshold itself: the model may break a tie, it may not manufacture
certainty. Tests never reach a model (``tests/conftest.py`` pins ``LLM_ENABLED=false``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from noc_agents.support.places import Gazetteer, PlaceMention
from noc_agents.support.policy import SupportPolicy
from noc_agents.support.text import clean, detect_language, extract_amounts, normalise, phrase_pattern

log = logging.getLogger(__name__)

Lexicon = tuple[tuple[str, float], ...]

# ------------------------------------------------------------------------------ lexicons

CATEGORY_LEXICON: dict[str, Lexicon] = {
    "mpesa": (
        ("mpesa", 2.0), ("wrong number", 2.0), ("wrong person", 2.0), ("wrong recipient", 2.0),
        ("namba mbaya", 2.0), ("nimekosea namba", 2.5), ("kimakosa", 1.5), ("nimetuma pesa", 2.5),
        ("nimetuma", 1.0), ("sent money", 2.0), ("send money", 1.5), ("sent", 0.5), ("reverse", 1.5),
        ("reversal", 2.0), ("rudisha pesa", 1.5), ("transaction code", 1.5), ("transaction", 1.0),
        ("paybill", 2.0), ("till number", 2.0), ("buy goods", 1.5), ("lipa na mpesa", 2.0),
        ("pesa haijafika", 2.5), ("haijafika", 1.0), ("not received", 1.0), ("pending", 1.0),
        ("fuliza", 1.5), ("withdraw", 1.0), ("agent", 0.5), ("pesa", 0.5),
        # payments that left the account and did not arrive (Kiswahili and Sheng verb forms)
        ("nililipa", 1.5), ("nimelipa", 1.5), ("nilipa", 1.5), ("lipa", 0.5), ("pesa imetoka", 2.0), ("pesa ikatoka", 2.0),
        ("pesa zimetoka", 2.0), ("money left my account", 2.0), ("money has left", 1.5), ("hajapokea", 2.0),
        ("hajapata", 1.5), ("hawajapokea", 2.0), ("haijaenda", 1.5), ("haikuenda", 1.5), ("till", 1.5), ("fare", 0.5),
        ("confirmed", 1.5), ("confirmation", 1.0), ("new mpesa balance", 1.0), ("sent to", 1.0), ("transfer", 1.0),
        ("transferred", 1.0), ("being processed", 1.5), ("processing", 1.0), ("token", 0.5), ("pochi", 1.5),
        ("mpesa balance", 1.5), ("namba isiyo sahihi", 2.5), ("namba isiyo", 2.0), ("namba noma", 2.0),
        ("wrong namba", 2.5), ("mtu nisiyemjua", 1.5), ("stranger", 1.0), ("rudishiwa pesa", 2.0), ("rudisheni pesa", 2.0),
        ("mrudishe pesa", 2.0), ("nilituma", 1.0), ("nilikosea", 1.5), ("kwa makosa", 1.5), ("by mistake", 1.0),
        ("sina code", 1.5), ("transaction code", 1.5), ("code", 0.5),
        ("lipa mdogo mdogo", -2.0), ("lipa pole pole", -2.0),
    ),
    "data_bundles": (
        ("bundle", 2.0), ("bundles", 2.0), ("bando", 2.0), ("data bundle", 1.0), ("data", 1.0),
        ("mbs", 1.5), ("gb", 1.0), ("expired early", 2.0), ("imeisha mapema", 2.0),
        ("zimeisha mapema", 2.0), ("zimeisha", 1.5), ("imeisha", 1.0), ("imeliwa", 1.5),
        ("haijaingia", 1.5), ("not applied", 1.5), ("data imeisha", 2.0), ("finished quickly", 1.5),
        ("mb", 1.0), ("bundle imeisha", 2.5), ("bundle zimeisha", 2.5), ("bundles zimeisha", 2.5), ("bando imeisha", 2.5),
        ("recredit", 2.0), ("re credit", 2.0), ("validity", 1.5), ("bundle expired", 2.5), ("expired", 1.0),
        ("showing expired", 2.0), ("already expired", 2.0), ("bundle balance", 2.0), ("data balance", 2.0),
        ("bundle haijaingia", 2.5), ("sijapata bundle", 2.5), ("daily bundle", 1.0), ("weekly bundle", 1.0),
        ("monthly bundle", 1.0), ("bundle ya", 1.0), ("mnakula bundles", 2.0), ("mnaiba data", 2.0),
        # contrast: being charged twice FOR a bundle is a billing matter
        ("charged twice", -2.0), ("double charge", -2.0), ("double charged", -2.0), ("deducted twice", -2.0),
        ("paid twice", -2.0), ("nimelipia mara mbili", -2.0), ("nililipa mara mbili", -2.0),
        # contrast: the customer HAS a bundle and still cannot use the network, or means personal data
        ("niko na bundle", -2.0), ("niko na bundles", -2.0), ("nina bundle", -2.0), ("nina bundles", -2.0),
        ("i have bundles", -2.0), ("i have a bundle", -2.0), ("i have bundle", -2.0), ("i have data", -1.5),
        ("with bundles", -1.5), ("niko na data", -1.5), ("personal data", -3.0), ("data commissioner", -3.0),
        ("data protection", -3.0), ("my data was shared", -3.0), ("data was shared", -3.0),
    ),
    "network": (
        ("no network", 3.0), ("network", 1.5), ("hakuna network", 3.0), ("mtandao", 2.0),
        ("hakuna mtandao", 3.0), ("signal", 2.0), ("no signal", 2.5), ("outage", 2.5),
        ("network down", 3.0), ("network iko chini", 3.0), ("network imepotea", 3.0),
        ("emergency calls only", 2.5), ("no service", 2.0), ("slow internet", 2.5), ("slow data", 2.5),
        ("cant call", 2.5), ("cannot call", 2.5), ("cant make calls", 2.5), ("cannot make calls", 2.5),
        ("receive calls", 1.5), ("make calls", 1.5), ("cant browse", 2.0), ("cannot browse", 2.0),
        ("network inapotea", 3.0), ("network haipo", 3.0),
        ("internet iko slow", 2.5), ("slow", 1.0), ("buffering", 1.5), ("call drops", 2.5),
        ("calls dropping", 2.5), ("dropping", 1.5), ("keep dropping", 2.0), ("dropped calls", 2.5),
        ("call drop", 2.5), ("zinakatika", 2.0),
        ("inakatika", 2.0), ("fibre", 2.5), ("fiber", 2.5), ("router", 2.0), ("wifi", 1.5),
        ("home internet", 2.5), ("internet", 0.5),
        # outages in Kiswahili and Sheng
        ("network imeenda", 3.0), ("mtandao haupo", 3.0), ("network haupo", 3.0), ("haupo", 1.0), ("umepotea", 1.5),
        ("hakuna bar", 2.5), ("no bars", 2.5), ("bars zimepotea", 2.5), ("bar moja", 1.5), ("bars", 1.0),
        ("haina signal", 2.5), ("haina network", 3.0), ("sina network", 3.0), ("sina signal", 2.5),
        ("network haifanyi", 3.0), ("mtandao haufanyi", 3.0), ("network imeisha", 2.5), ("network inasumbua", 2.5),
        ("network mbaya", 2.5), ("poor network", 2.5), ("weak signal", 2.5), ("signal iko chini", 2.5), ("sos only", 2.5),
        ("mtandao umepotea", 3.0), ("hamna network", 3.0), ("hamna mtandao", 3.0), ("hakuna huduma", 2.0),
        ("no reception", 2.5), ("no coverage", 2.5), ("coverage", 1.5), ("reception", 1.5), ("network imekufa", 3.0),
        ("network iko down", 3.0), ("network is down", 3.0), ("cant receive calls", 2.5), ("cannot receive calls", 2.5),
        ("calls not going through", 2.5), ("simu haipiti", 2.5), ("simu haziingii", 2.5), ("haziingii", 1.5),
        ("kupiga simu haiwezekani", 2.5),
        # calls cutting (Sheng)
        ("zinakata", 2.0), ("inakata", 2.0), ("simu zinakata", 2.5), ("simu inakata", 2.5), ("call inakata", 2.5),
        ("calls zinakata", 2.5), ("inajikata", 2.5), ("cuts off", 2.0), ("cut off", 1.5),
        # slow or dead data while a bundle is loaded
        ("iko slow", 2.0), ("ni slow", 1.5), ("slow sana", 2.0), ("haziload", 2.0), ("haiload", 2.0), ("inaload", 1.5),
        ("haifunguki", 2.0), ("hazifunguki", 2.0), ("hakuna page", 2.0), ("not loading", 2.0), ("pages not loading", 2.5),
        ("siwezi browse", 2.5), ("siwezi kubrowse", 2.5), ("data haifanyi", 2.5), ("data not working", 2.0),
        ("internet haifanyi", 2.5), ("4g", 1.0), ("3g", 0.5), ("upload", 1.0), ("download", 1.0),
        # contrast: another network is a porting question, not an outage
        ("network ingine", -2.0), ("network nyingine", -2.0), ("network mwingine", -2.0), ("another network", -2.0),
        ("mtandao mwingine", -2.0), ("mtandao ingine", -2.0), ("other network", -1.5),
        # contrast: no internet on a NEW phone, or a question about the APN, is device settings
        ("new phone", -2.0), ("simu mpya", -2.0), ("apn", -2.0),
    ),
    "billing": (
        ("airtime", 1.5), ("credo", 1.5), ("salio", 1.5), ("deducted", 1.5), ("imekatwa", 1.5),
        ("nimekatwa", 1.5), ("zimekatwa", 1.5), ("refund", 1.5), ("charged", 1.5), ("charge", 1.0),
        ("charged twice", 3.0), ("double charge", 3.0), ("double charged", 3.0), ("deducted twice", 3.0),
        ("mara mbili", 2.0), ("premium", 2.0), ("subscription", 2.0), ("subscribed", 1.5),
        ("unsubscribe", 2.0), ("sms za ajabu", 2.5), ("messages za ajabu", 2.5), ("betting tips", 1.5),
        ("bill", 2.0), ("bili", 2.0), ("postpaid", 1.5), ("invoice", 2.0), ("okoa", 2.5),
        ("okoa jahazi", 1.0), ("airtime advance", 3.0), ("borrowed airtime", 3.0), ("deni", 1.5),
        ("nilikopa", 2.0), ("top up", 0.5),
        ("billing", 1.5), ("credo imeliwa", 3.0), ("credo inaliwa", 3.0), ("airtime imeliwa", 3.0), ("airtime inaliwa", 3.0),
        ("salio imeliwa", 3.0), ("credo yangu", 1.0), ("credo zetu", 1.0), ("mnakula credo", 2.5), ("mnakula airtime", 2.5),
        ("airtime disappeared", 2.5), ("airtime vanished", 2.5), ("airtime imepotea", 2.5), ("airtime inapotea", 2.5),
        ("airtime disappearing", 2.5), ("credit deducted", 2.0), ("horoscope", 2.0), ("jokes", 1.0), ("mambo ya nyota", 2.0),
        ("nyota", 1.0), ("nakatwa", 1.5), ("nakatwa kila siku", 2.5), ("stop these messages", 2.0), ("stop messages", 2.0),
        ("stop the messages", 2.0), ("mniondoe", 2.0), ("niondoe", 1.5), ("jiondoa", 2.0), ("ondoeni", 1.5),
        ("sikujiunga", 2.0), ("sijajiunga", 2.0), ("never subscribed", 2.0), ("sijawahi subscribe", 2.0),
        ("sijawahi kujiunga", 2.0), ("kujiunga", 1.5), ("statement", 1.5), ("itemised", 2.0), ("itemized", 2.0),
        ("overcharged", 2.0), ("niliokoa", 2.5), ("nilichukua okoa", 2.0), ("top up imekatwa", 2.5), ("without calling", 1.5),
        ("made no calls", 1.5), ("sijapiga", 1.5), ("bila kupiga", 1.5), ("sijapiga simu", 2.0), ("shillings", 0.5),
        ("bob", 0.5), ("my bill", 1.0), ("bill yangu", 2.0), ("bili yangu", 2.0),
    ),
    "sim_and_fraud": (
        ("sim swap", 3.0), ("swap", 2.0), ("imeswapiwa", 3.0), ("fraud", 3.0), ("puk", 3.0),
        ("sim", 1.0), ("laini", 1.0), ("stolen", 2.5), ("imeibiwa", 2.5), ("nimeibiwa", 2.5),
        ("lost my phone", 3.0), ("lost phone", 3.0), ("nimepoteza simu", 3.0), ("simu imepotea", 3.0),
        ("block my line", 2.5), ("block the line", 2.5), ("funga laini", 2.5), ("sim replacement", 2.5),
        ("replace my sim", 2.5), ("sim locked", 2.5), ("imejifunga", 2.5), ("hacked", 2.5), ("pin", 1.0),
        ("sim card", 1.5), ("damaged", 1.0), ("not detected", 1.5), ("replace", 1.0), ("laini imeharibika", 2.5),
        ("snatched", 2.5), ("robbed", 2.5), ("mugged", 2.5), ("police", 1.0), ("police abstract", 1.5),
        ("block the sim", 2.5), ("block my sim", 2.5), ("block my number", 2.5), ("funga line", 2.5), ("fungeni line", 2.5),
        ("fungeni laini", 2.5), ("zima laini", 2.5), ("zima line", 2.5), ("wameiba", 2.0), ("iliibiwa", 2.5),
        ("no sim", 2.0), ("sim not detected", 2.5), ("no sim card", 2.5), ("insert sim", 2.0), ("pin mbaya", 1.0),
        ("wrong pin", 1.0), ("new sim", 1.5), ("replacement sim", 2.5), ("sim yangu", 1.0), ("scam", 3.0),
        ("scammed", 3.0), ("scammer", 3.0), ("nimeconiwa", 3.0), ("conned", 3.0), ("conman", 3.0), ("tapeliwa", 3.0),
        ("nimetapeliwa", 3.0), ("nimeibiwa pesa", 3.0), ("pesa zimeibiwa", 3.0), ("money stolen", 3.0),
        ("stolen from my mpesa", 3.0), ("ametoa pesa", 2.5), ("alitoa pesa", 2.5), ("ameingia mpesa", 3.0),
        ("ameingia kwa mpesa", 3.0), ("ameingia kwenye mpesa", 3.0), ("sim imebadilishwa", 3.0),
        ("sim yangu imebadilishwa", 3.0), ("laini imebadilishwa", 3.0), ("sim was replaced", 3.0), ("line was replaced", 3.0),
        ("sikuwa na simu", 2.0), ("customer care", 0.5),
    ),
    "device_settings": (
        ("apn", 4.0), ("internet settings", 3.0), ("settings", 1.5), ("mms", 2.5), ("configure", 2.0),
        ("configuration", 2.0), ("configuring", 2.0), ("new phone", 3.0), ("simu mpya", 3.0), ("simu yangu mpya", 3.0),
        ("my new phone", 3.0), ("hotspot", 1.5), ("set up", 1.0),
        ("send settings", 2.5), ("send me settings", 2.5), ("send me the settings", 2.5), ("nitumie settings", 3.0),
        ("nitumieni settings", 3.0), ("tuma settings", 3.0), ("settings za internet", 3.0), ("settings za mms", 3.0),
        ("settings za data", 3.0), ("config", 2.0), ("configs", 2.0), ("nimenunua simu", 2.0), ("bought a new phone", 2.5),
        ("got a new phone", 2.5), ("new handset", 2.5), ("manual", 1.0), ("manually", 1.0), ("picha kwa message", 1.5),
        ("send pictures", 1.5), ("picture messages", 2.0), ("haina internet", 1.0), ("no internet", 0.5),
    ),
    "roaming": (
        ("roaming", 3.5), ("abroad", 2.5), ("nje ya nchi", 2.5), ("travelling", 1.5), ("traveling", 1.5),
        ("travel", 1.0), ("uganda", 2.0), ("tanzania", 2.0), ("rwanda", 2.0), ("dubai", 2.0),
        ("kampala", 2.0), ("dar es salaam", 2.0), ("kigali", 2.0), ("arusha", 2.0), ("juba", 2.0), ("ethiopia", 2.0),
        ("south africa", 2.0), ("london", 2.0), ("qatar", 2.0), ("doha", 2.0), ("saudi", 2.0), ("nje ya kenya", 2.5),
        ("will my line work", 2.0), ("will my phone work", 2.0), ("itafanya kazi huko", 2.0), ("flying", 1.5), ("fly to", 1.5),
        ("trip", 1.0), ("holiday", 1.0), ("vacation", 1.0),
        ("nasafiri", 1.5), ("nikisafiri", 1.5), ("ninasafiri", 1.5), ("kusafiri", 1.5), ("niko nje", 2.0),
        ("travelling to", 2.0), ("international", 1.5), ("roaming bundle", 1.0), ("came back", 1.0), ("nikirudi", 1.0),
    ),
    "account": (
        ("register", 2.0), ("registration", 2.5), ("haijasajiliwa", 3.0), ("sajili", 2.5),
        ("kuhamia mtandao", 3.0), ("hamia mtandao", 3.0), ("nibaki na namba", 3.0),
        ("ownership", 2.5), ("owner of the line", 3.0), ("owner of my line", 3.0), ("id number", 1.5),
        ("porting", 3.0), ("port my number", 3.5), ("move my number", 3.0), ("keep my number", 3.0),
        ("switch network", 2.5), ("another network", 1.5), ("hamia", 2.0), ("mnp", 3.0),
        ("registered", 2.0), ("not registered", 3.0), ("unregistered", 3.0), ("haijaregister", 3.0), ("kusajili", 2.5),
        ("usajili", 2.5), ("jina langu", 2.0), ("kwa jina", 1.5), ("jina la", 1.0), ("in my name", 2.5), ("under my name", 2.5),
        ("to my name", 2.5), ("badilisha jina", 2.5), ("change the name", 2.0), ("change of name", 2.0), ("hamisha", 1.5),
        ("kuihamisha", 2.0), ("transfer the line", 2.5), ("transfer this line", 2.5), ("transfer my line", 2.5),
        ("deceased", 2.0), ("aliyefariki", 2.0), ("alifariki", 2.0), ("marehemu", 2.0), ("death certificate", 2.5),
        ("passed away", 2.0), ("my id", 1.0), ("kitambulisho", 2.0), ("owner", 1.5), ("mwenye laini", 2.0), ("mmiliki", 2.0),
        ("kuhama", 2.5), ("hama kwenda", 2.5), ("kuhamia", 2.5), ("network ingine", 2.5), ("network nyingine", 2.5),
        ("network mwingine", 2.0), ("mtandao mwingine", 2.5), ("mtandao ingine", 2.5), ("nibaki na number", 3.0),
        ("nibaki na line", 3.0), ("nibaki na laini", 3.0), ("namba yangu ibaki", 3.0), ("number yangu ibaki", 3.0),
        ("keep my old number", 3.0), ("same number", 2.0), ("retain my number", 3.0), ("port in", 2.5), ("port out", 2.5),
        ("coming from another network", 3.0), ("old number", 1.5), ("nimehamia", 2.5), ("kutoka ingine", 1.5),
        ("kutoka network ingine", 2.5), ("namba ya zamani", 2.0), ("number ya zamani", 2.0),
    ),
    "other": (
        ("regulator", 3.0), ("escalate my complaint", 3.0), ("escalate", 1.5), ("not satisfied", 2.0),
        ("sijaridhika", 2.5), ("where else can i complain", 3.0), ("complaint was handled", 2.0),
        ("privacy", 2.0), ("personal data", 3.0), ("data commissioner", 2.5), ("data protection", 2.5),
        ("shared my number", 2.5), ("shared my data", 2.5), ("shared my details", 2.5), ("third parties", 1.5),
        ("marketers", 1.5), ("telemarketing", 1.5), ("consent", 0.5), ("malalamiko", 2.0), ("nipeleke wapi", 2.0),
        ("kupeleka wapi", 2.0), ("kulalamika", 2.0), ("not happy with", 1.5), ("final response", 2.0), ("ombudsman", 2.0),
        ("consumer", 1.0), ("supervisor", 1.0), ("manager", 1.0),
    ),
}

#: Risk flags, keyed by the escalation reason code they raise.
RISK_LEXICON: dict[str, tuple[str, ...]] = {
    "fraud_or_sim_swap": (
        "sim swap", "simswap", "swapped my sim", "sim was swapped", "line was swapped", "imeswapiwa",
        "swapiwa", "laini yangu imebadilishwa", "someone is using my mpesa", "someone used my mpesa",
        "mtu anatumia mpesa yangu", "pin changed", "pin was changed", "pin yangu imebadilishwa",
        "unknown pin change", "did not change my pin", "sikubadilisha pin", "account takeover", "hacked",
        "fraud", "fraudster", "conman", "con man", "mlaghai", "walaghai", "scammed", "unauthorised withdrawal",
        "unauthorized withdrawal", "unauthorised transaction", "unauthorized transaction",
        "without my knowledge", "pretending to be customer care", "pretended to be customer care",
        "withdrawal i did not make", "transaction i did not make", "transactions i did not make",
        "payment i did not make", "did not authorise", "did not authorize", "didnt authorise", "didnt authorize",
        "not authorised by me", "sikutoa pesa", "sijatoa pesa", "transactions i never did",
        "transaction i never did", "transactions i never made", "money missing from my mpesa",
        "bila mimi kujua", "bila kujua kwangu", "sikuwa na simu", "nimeconiwa", "conned", "tapeliwa", "nimetapeliwa",
        "scam", "scammer", "fake customer care", "akijifanya customer care", "akisema ni wa customer care",
        "money stolen", "pesa zimeibiwa", "nimeibiwa pesa", "wameiba pesa", "stolen from my mpesa", "ameiba pesa",
        "someone is using my line", "someone is using my number", "my number is on whatsapp",
        "number is active on whatsapp", "sim imebadilishwa", "sim yangu imebadilishwa", "laini imebadilishwa",
        "ametoa pesa", "alitoa pesa", "ameingia mpesa", "ameingia kwa mpesa", "ameingia kwenye mpesa",
    ),
    "legal_or_regulator": (
        "sue you", "sue", "suing", "sued", "lawsuit", "law suit", "legal action", "take legal", "legal proceedings",
        "legal team", "legal department", "file a suit", "file suit", "filing a suit", "demand letter",
        "letter of demand", "formal demand", "demand notice", "notice of demand", "communications authority", "odpc",
        "data commissioner", "data protection commissioner", "small claims", "mahakama", "mahakamani", "kortini",
        "to court", "in court", "court case", "court order", "court action", "court summons", "kesi", "nitafungua kesi",
        "kufungua kesi", "nitawashtaki", "kushtaki", "tribunal", "consumer protection",
    ),
    "threat_or_safety": (
        "kill myself", "end my life", "suicide", "suicidal", "kujiua", "nitajiua", "harm myself",
        "hurt myself", "self harm", "kill you", "i will kill", "nitakuua", "nitawaua", "burn your",
        "bomb", "threatening me", "threatened me", "threatens me", "threatening messages", "ananitishia",
        "wananitishia", "vitisho", "harassing me", "harassment", "harass", "stalking", "stalker",
        "blackmail", "blackmailing",
        # violence towards staff or property
        "gets hurt", "get hurt", "be hurt", "hurt you", "hurt someone", "will hurt", "beat you", "beat up",
        "nitakupiga", "nitawapiga", "nitampiga", "teach you a lesson", "you will regret", "mtajua", "mtanijua",
        "nitawaonyesha", "nitakuonyesha", "deal with you", "come for you", "attack", "stab", "shoot you",
        "burn it down", "burn down", "nitachoma", "nitawachoma", "nitakuchoma", "destroy your", "smash", "violence",
        "kill someone", "kill them", "nitaua", "kuua",
        # self-harm
        "hang myself", "take my life", "end it all", "ending it all", "sina sababu ya kuishi", "afadhali nife",
        "bora nife", "heri nife", "nife tu", "nijiue", "nitajinyonga", "overdose", "cut myself", "want to die",
        "i want to die", "nataka kufa", "no reason to live", "nothing to live for", "not worth living",
        # harassment and abuse by callers
        "ananitusi", "akinitusi", "anitusi", "kunitusi", "kunitishia", "akinitishia", "ananisumbua", "wananisumbua",
        "ananifuata", "threatening calls", "threatening sms", "abusive messages", "abusive calls", "insulting me",
        "insults me", "kuniumiza", "ataniumiza", "atanidhuru", "naogopa", "i am scared", "im scared",
        "scared for my life", "fearing for my life", "fear for my",
    ),
}
#: Professions and institutions that are a legal flag only beside a cue that an action against the
#: operator is meant: "my lawyer will contact you" is, "I paid my lawyer via paybill" is not.
LEGAL_WEAK_NOUNS: frozenset[str] = frozenset({
    "lawyer", "lawyers", "advocate", "advocates", "wakili", "mawakili", "attorney", "attorneys", "solicitor",
    "solicitors", "court", "courts",
})
LEGAL_CUES = re.compile(
    r"\b(?:will|shall|going to|gonna|contact\w*|instruct\w*|involv\w*|engag\w*|hire\w*|hiring|consult\w*|"
    r"through (?:my|the|our)|refer\w*|fil(?:e|ed|ing)|sue|suing|sued|action|letter|demand|notice|hear from|"
    r"talk to|speak to|speaking to|call(?:ing)? my|see you|further|pursue|proceed\w*|escalat\w*|take (?:this|you|the|it)|"
    r"handle|deal with|report\w*|nita\w*|atawa\w*|watawa\w*|kesi|mahakama\w*|shtaki|kushtaki|summon\w*|order)\b"
)
LEGAL_CUE_WINDOW = 8
#: Patterns for what a fraud complaint describes rather than the words it uses: somebody did
#: something to the customer's M-PESA, PIN, SIM or line; a SIM or PIN that "was changed".
RISK_REGEX: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "fraud_or_sim_swap": (
        ("someone did something to my account", re.compile(
            r"\b(?:mtu|mse|msee|jamaa|someone|somebody|flani|fulani|wezi|thief|thieves|hackers?|watu)\b.{0,40}?"
            r"\b(?:ameingia|aliingia|wameingia|ametoa|alitoa|wametoa|amechukua|alichukua|wamechukua|anatumia|alitumia|"
            r"ametumia|wanatumia|withdrew|withdrawn|took|taken|accessed|logged|is using|was using|used|changed|emptied|"
            r"drained|cleared)\b.{0,40}?\b(?:mpesa|pin|line|laini|sim|account|akaunti|balance|money|pesa|savings|number|namba)\b")),
        ("the SIM or line was swapped", re.compile(
            r"\b(?:sim|laini|line|number|namba)\b(?:\s+\w+){0,3}?\s+(?:imebadilishwa|ilibadilishwa|zimebadilishwa|was replaced|"
            r"been replaced|was changed|imeswapiwa|swapped|was swapped|got swapped|imechukuliwa|was taken over|taken over|hijacked)\b")),
        ("the PIN was changed", re.compile(
            r"\bpin\b(?:\s+\w+){0,3}?\s+(?:imebadilishwa|ilibadilishwa|was changed|has been changed|got changed)\b")),
        ("an impersonator", re.compile(
            r"\b(?:akijifanya|anajifanya|alijifanya|pretending to be|pretended to be|claiming to be|claimed to be|posing as|"
            r"impersonat\w+)\b.{0,30}?\b(?:customer care|operator|staff|agent|bank|mpesa|you|safaricom)\b")),
    ),
}
#: A risk word the customer is denying ("I don't think this is fraud, I typed the number wrong") is
#: masked before scoring, so neither the flag nor the fraud category reads it as evidence.
NEGATED_RISK = re.compile(
    r"\b(?:not|no|never|si|sio|siyo|isnt|wasnt|hakuna|hii si|hii sio|sidhani ni|sio kama ni|"
    r"dont think (?:this|it|that) is|do not think (?:this|it|that) is|dont think its|not really)"
    r"\s+(?:a\s+|an\s+|hii\s+|ni\s+|really\s+|even\s+)?(?:fraud|scam|fraudster|conman|con man|mlaghai|scammed|hacked|stolen)\b"
)
#: Abbreviations matched case-sensitively on the ORIGINAL text.
CASE_SENSITIVE_RISK: dict[str, tuple[str, ...]] = {"legal_or_regulator": ("CA", "CAK")}

URGENT_WORDS: tuple[str, ...] = (
    "urgent", "urgently", "emergency", "asap", "immediately", "haraka", "sasa hivi", "business",
    "biashara", "hospital", "losing money", "critical",
)
QUESTION_OPENERS: tuple[str, ...] = (
    "how do i", "how can i", "how to", "what is", "what are", "can i", "is it possible",
    "jinsi ya", "naweza aje", "nawezaje", "ninawezaje", "nifanye nini", "where can i",
)
#: Anger words with weights; ``angry`` needs a total of 2 (one strong word, or two milder).
ANGER_LEXICON: Lexicon = (
    ("thieves", 2.0), ("wezi", 2.0), ("idiots", 2.0), ("matapeli", 2.0), ("furious", 2.0),
    ("fed up", 2.0), ("useless", 2.0), ("nonsense", 2.0), ("upuzi", 2.0), ("ujinga", 2.0),
    ("rubbish", 2.0), ("pathetic", 2.0), ("stupid", 2.0), ("shame on you", 2.0), ("mmeniibia", 2.0),
    ("nimekasirika", 2.0), ("very angry", 2.0), ("angry", 1.0), ("hasira", 1.0), ("worst", 1.0),
    ("terrible", 1.0), ("ridiculous", 1.0), ("disgusted", 1.0), ("unacceptable", 1.0),
    ("mnaiba", 2.0), ("stealing", 1.5), ("thief", 2.0), ("what is wrong with you", 2.0), ("are you serious", 1.0),
    ("hopeless", 2.0), ("incompetent", 2.0), ("scammers", 2.0), ("con artists", 2.0), ("wakora", 2.0), ("mafala", 2.0),
    ("mjinga", 2.0), ("wajinga", 2.0), ("takataka", 2.0), ("shenzi", 2.0), ("mnakula", 1.0), ("absolutely", 0.5),
)
FRUSTRATION_WORDS: tuple[str, ...] = (
    "again", "still", "bado", "tena", "third time", "mara ya tatu", "disappointed", "nimechoka",
    "kila siku", "since yesterday", "tangu jana", "for days", "siku tatu", "no one is helping",
    "nobody is helping", "waiting", "frustrated", "annoying", "imagine", "honestly", "bado haijarudi", "nimeripoti",
    "mara mbili", "wiki hii", "every day", "kila asubuhi", "sick of", "tired of",
)

# ------------------------------------------------------------------------------- intents
# An intent is what the action agent could DO. Each needs its category to have won and one of
# its trigger phrases; "outage" also needs a place the gazetteer knows.

INTENTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # intent: (category, tool, trigger phrases)
    "reverse_mpesa": ("mpesa", "reverse_mpesa", (
        "wrong number", "wrong person", "wrong recipient", "namba mbaya", "nimekosea namba", "kimakosa",
        "reverse", "reversal", "rudisha", "mtu mwingine", "mistake", "mistakenly", "by mistake",
        "namba isiyo sahihi", "namba noma", "wrong namba", "nilikosea", "nimekosea", "mtu nisiyemjua", "stranger",
        "mnirudishie", "nirudishie", "rudisheni", "mrudishe", "irudi", "undo", "kwa makosa",
    )),
    "recredit_bundle": ("data_bundles", "recredit_bundle", (
        "expired early", "imeisha mapema", "zimeisha mapema", "not applied", "haijaingia", "not received",
        "never received", "didnt get", "did not get", "sikupata", "sijapata", "expired before",
        "finished quickly", "finished fast", "ran out fast", "zimeisha haraka", "imeisha haraka", "recredit",
        "re credit", "within a day", "after one day", "baada ya siku moja", "already expired", "showing expired",
        "shows expired", "says expired", "inasema expired", "inaonyesha expired", "cannot be right", "kabla ya muda",
        "kabla ya siku", "before validity", "before its validity", "before the validity", "after 1 day", "after a day",
        "within 1 day", "ndani ya siku moja", "hazijaingia", "haikuingia", "sijaona bundle", "bundle hakuna",
        "no bundle", "nothing came", "haijareflect", "has not reflected", "not reflected", "hasnt reflected",
        "haijaonekana", "restore", "give me back my data", "return my data", "rudisha bundle", "rudisheni bundle",
        "nirudishie bundle", "mnirudishie data", "used barely", "barely used", "sijatumia", "hardly used",
        "died after", "finished after", "imeisha baada ya", "zimeisha baada ya", "expired after", "ndani ya masaa",
        "within hours", "in an hour", "ndani ya saa",
    )),
    "issue_refund": ("billing", "issue_refund", (
        "refund", "rudisha", "rudisheni", "charged twice", "double charge", "double charged", "deducted twice",
        "mara mbili", "never subscribed", "did not subscribe", "didnt subscribe", "sikujiunga",
        "without my consent", "airtime deducted", "airtime imekatwa", "credo imekatwa", "salio imekatwa",
        "nimekatwa", "deducted without", "bila sababu", "for no reason", "without reason", "nirudishie",
        "mnirudishie", "give me back my airtime", "credo imeliwa", "airtime imeliwa", "salio imeliwa", "imeliwa",
        "inaliwa", "mnakula", "vanished", "disappeared", "imepotea", "inapotea", "disappearing", "sijapiga",
        "bila kupiga", "without calling", "made no calls", "sikupiga", "without using", "deducted", "imekatwa",
        "zimekatwa", "nakatwa", "nilikatwa", "wamekata", "mmekata", "charged me", "charged for", "overcharged", "double",
        "twice", "duplicate", "sikuwahi", "sijawahi", "sikuomba", "sijaomba", "never asked", "did not ask", "didnt ask",
        "money back", "pesa yangu irudi", "pesa irudi", "rudisha pesa yangu", "rudisheni pesa", "want it back",
        "irudishwe",
    )),
    "link_incident": ("network", "link_incident", (
        "no network", "hakuna network", "hakuna mtandao", "network down", "no signal", "hakuna signal",
        "network imepotea", "network iko chini", "outage", "emergency calls only", "no service",
        "network problem", "network issues", "mtandao umepotea", "network inapotea", "network haipo",
        "cant call", "cannot call", "cant make calls", "cannot make calls",
        "network imeenda", "mtandao haupo", "network haupo", "haupo", "hakuna bar", "no bars", "bars zimepotea",
        "haina signal", "haina network", "sina network", "sina signal", "network haifanyi", "mtandao haufanyi",
        "umepotea", "imepotea", "network imeisha", "network inasumbua", "poor network", "no reception", "no coverage",
        "weak signal", "signal iko chini", "sos only", "cant receive calls", "cannot receive calls",
        "calls not going through", "simu haipiti", "simu haziingii", "haziingii", "hakuna huduma", "network is down",
        "network imekufa", "network iko down", "iko down", "hamna network", "hamna mtandao", "down since", "off since",
        "offline", "kupiga simu haiwezekani",
    )),
    "reset_network_settings": ("device_settings", "reset_network_settings", (
        "apn", "internet settings", "mms", "settings", "configure", "configuration", "set up", "setup",
        "send me", "send the", "nitumie", "nitumieni", "tuma", "config", "configs", "haina internet", "no internet",
        "data not working", "internet haifanyi", "data haifanyi",
    )),
}
#: Words that mean a billing complaint is NOT asking for a refund (repayment, bill queries).
REFUND_EXCLUSIONS: tuple[str, ...] = (
    "airtime advance", "borrowed airtime", "okoa", "niliokoa", "nilichukua okoa", "nilikopa", "advance", "mkopo", "deni",
    "loan", "postpaid bill", "my bill", "bill yangu", "bili yangu", "bili", "statement", "itemised", "itemized", "invoice",
    "postpaid",
)
#: Words that mean the customer wants to set the phone up by hand or asks how, not the configuration SMS.
SETTINGS_EXCLUSIONS: tuple[str, ...] = (
    "manually", "manual", "by hand", "where do i find", "how do i set", "how to set", "steps", "mwenyewe", "kwa mkono",
    "type them in", "enter them", "what is the apn", "what apn", "apn do i use", "apn should i", "apn name", "which apn",
    "apn ni gani", "ni apn gani", "apn gani", "apn ya",
    # the configuration SMS goes to a phone, not to these
    "router", "modem", "mifi", "laptop", "computer", "pc", "desktop",
)


def _compile(phrases: tuple[str, ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((p, phrase_pattern(p)) for p in phrases)


_CATEGORY_PATTERNS = {
    cat: tuple((p, w, phrase_pattern(p)) for p, w in lex) for cat, lex in CATEGORY_LEXICON.items()
}
_RISK_PATTERNS = {code: _compile(phrases) for code, phrases in RISK_LEXICON.items()}
_CASE_PATTERNS = {
    code: tuple((p, re.compile(r"(?<![A-Za-z])" + re.escape(p) + r"(?![A-Za-z])")) for p in phrases)
    for code, phrases in CASE_SENSITIVE_RISK.items()
}
_URGENT = _compile(URGENT_WORDS)
_QUESTION = _compile(QUESTION_OPENERS)
_ANGER = tuple((p, w, phrase_pattern(p)) for p, w in ANGER_LEXICON)
_FRUSTRATION = _compile(FRUSTRATION_WORDS)
_INTENT_PATTERNS = {name: (cat, tool, _compile(ph)) for name, (cat, tool, ph) in INTENTS.items()}
_REFUND_EXCLUSIONS = _compile(REFUND_EXCLUSIONS)
_SETTINGS_EXCLUSIONS = _compile(SETTINGS_EXCLUSIONS)
_SHOUTING = re.compile(r"!{2,}")

PRIOR = 1.0  # the "+ 1" in the confidence formula (module docstring)
#: Customers lead with the complaint and add the rest after a connector ("Hakuna network Eldoret,
#: na pia nataka kujua deni yangu ya okoa"). Evidence after the first such connector counts
#: half, so a two-issue message is classified by the issue it leads with instead of tying.
SECONDARY_ISSUE = re.compile(
    r"\b(?:na pia|and also|and another thing|another thing|secondly|second issue|in addition|as well as|pia nataka|"
    r"pia nilitaka|halafu|kisha|plus i|also i wanted|also i want|and i also|i also wanted|i also want|na vile vile|vilevile)\b"
)
SECONDARY_WEIGHT = 0.5
#: A fraud flag is also evidence for the fraud CATEGORY: "someone is using my M-PESA" is an
#: account-takeover complaint even though the only category word in it is "M-PESA".
FRAUD_CATEGORY_WEIGHT = 3.0
MONEY_HIGH_URGENCY_KES = 1000


# ------------------------------------------------------------------------------- result


@dataclass
class TriageResult:
    category: str
    confidence: float
    urgency: str
    sentiment: str
    language: str
    risk_flags: tuple[str, ...]
    intent: str | None
    tool: str | None
    route: str
    places: tuple[PlaceMention, ...]
    scores: dict[str, float]
    reasons: list[str] = field(default_factory=list)
    source: str = "rules"  # rules | llm_tiebreak

    def detail(self) -> dict[str, Any]:
        """The step trace's structured detail."""
        return {
            "category": self.category,
            "confidence": self.confidence,
            "urgency": self.urgency,
            "sentiment": self.sentiment,
            "language": self.language,
            "risk_flags": list(self.risk_flags),
            "intent": self.intent,
            "tool": self.tool,
            "route": self.route,
            "places": [{"name": p.name, "regions": list(p.regions)} for p in self.places],
            "scores": {k: v for k, v in sorted(self.scores.items(), key=lambda kv: -kv[1]) if v > 0},
            "reasons": self.reasons,
            "source": self.source,
        }


# ------------------------------------------------------------------------------ helpers


def _hits(norm: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]) -> list[str]:
    return [phrase for phrase, pattern in patterns if pattern.search(norm)]


def _score_part(norm: str, weight: float, scores: dict[str, float], fired: dict[str, list[str]]) -> None:
    for category, patterns in _CATEGORY_PATTERNS.items():
        matched = [(p, w) for p, w, pattern in patterns if pattern.search(norm)]
        scores[category] = scores.get(category, 0.0) + weight * sum(w for _, w in matched)
        fired.setdefault(category, []).extend(p for p, _ in matched if p not in fired.get(category, []))


def score_categories(norm: str) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Every category's summed phrase weight, and which phrases fired for it. Text after the first
    :data:`SECONDARY_ISSUE` connector counts :data:`SECONDARY_WEIGHT`."""
    scores: dict[str, float] = {}
    fired: dict[str, list[str]] = {}
    split = SECONDARY_ISSUE.search(norm)
    if split:
        _score_part(norm[: split.start()], 1.0, scores, fired)
        _score_part(norm[split.start():], SECONDARY_WEIGHT, scores, fired)
    else:
        _score_part(norm, 1.0, scores, fired)
    return {c: round(v, 3) for c, v in scores.items()}, fired


def confidence_of(scores: dict[str, float]) -> tuple[str, float]:
    """The winning category and ``top / (top + runner_up + PRIOR)``; no evidence is ``other`` at 0."""
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    top_cat, top = ranked[0]
    if top <= 0:
        return "other", 0.0
    runner_up = max(0.0, ranked[1][1]) if len(ranked) > 1 else 0.0  # a negative (contrast) score is no evidence
    return top_cat, round(top / (top + runner_up + PRIOR), 3)


def _weak_legal_hits(norm: str) -> list[str]:
    """A weak legal noun with an action cue within :data:`LEGAL_CUE_WINDOW` words of it."""
    words, hits = norm.split(), []
    for i, word in enumerate(words):
        if word in LEGAL_WEAK_NOUNS:
            window = " ".join(words[max(0, i - LEGAL_CUE_WINDOW): i + LEGAL_CUE_WINDOW + 1])
            cue = LEGAL_CUES.search(window)
            if cue:
                hits.append(f"{word} ({cue.group(0)})")
    return hits


def risk_flags(text: str, norm: str) -> dict[str, list[str]]:
    """Reason code -> the phrases that raised it (only codes that fired)."""
    flags: dict[str, list[str]] = {}
    for code, patterns in _RISK_PATTERNS.items():
        hits = _hits(norm, patterns)
        hits += [p for p, pattern in _CASE_PATTERNS.get(code, ()) if pattern.search(text)]
        hits += [label for label, pattern in RISK_REGEX.get(code, ()) if pattern.search(norm)]
        if code == "legal_or_regulator":
            hits += _weak_legal_hits(norm)
        if hits:
            flags[code] = hits
    return flags


def sentiment_of(text: str, norm: str) -> tuple[str, list[str]]:
    """``angry`` (anger weight >= 2), ``frustrated`` (some anger or a frustration word), else ``calm``."""
    fired = [(p, w) for p, w, pattern in _ANGER if pattern.search(norm)]
    weight = sum(w for _, w in fired)
    signals = [p for p, _ in fired]
    if _SHOUTING.search(text):
        weight += 1
        signals.append("!!")
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 12 and sum(c.isupper() for c in letters) / len(letters) >= 0.6:
        weight += 1
        signals.append("CAPITALS")
    if weight >= 2:
        return "angry", signals
    frustration = _hits(norm, _FRUSTRATION)
    if weight > 0 or frustration:
        return "frustrated", signals + frustration
    return "calm", []


def urgency_of(norm: str, category: str, flags: dict[str, list[str]], amounts: list[int]) -> tuple[str, list[str]]:
    """critical: fraud or safety; high: legal, urgency words, or money over KES 1,000 at stake;
    low: a plain how-to question; normal otherwise."""
    if "fraud_or_sim_swap" in flags or "threat_or_safety" in flags:
        return "critical", ["risk flag"]
    urgent = _hits(norm, _URGENT)
    big_money = category in ("mpesa", "billing") and any(a >= MONEY_HIGH_URGENCY_KES for a in amounts)
    if "legal_or_regulator" in flags or urgent or big_money:
        return "high", urgent + (["amount >= KES 1,000"] if big_money else []) + (["legal flag"] if "legal_or_regulator" in flags else [])
    if any(pattern.match(norm) for _, pattern in _QUESTION):
        return "low", ["how-to question"]
    return "normal", []


def intent_of(norm: str, category: str, places: list[PlaceMention]) -> tuple[str | None, str | None, list[str]]:
    """The action intent for the winning category, its tool, and the trigger phrases that fired."""
    for name, (intent_category, tool, patterns) in _INTENT_PATTERNS.items():
        if intent_category != category:
            continue
        triggers = _hits(norm, patterns)
        if not triggers:
            continue
        if name == "link_incident" and not places:
            continue  # an outage with no place cannot be matched to a ticket: the resolver answers
        if name == "issue_refund" and _hits(norm, _REFUND_EXCLUSIONS):
            continue
        if name == "reset_network_settings" and _hits(norm, _SETTINGS_EXCLUSIONS):
            continue
        return name, tool, triggers
    return None, None, []


# ------------------------------------------------------------------------- LLM tie-break


class TieBreak(BaseModel):
    category: str
    reason: str = ""


TIEBREAK_SYSTEM = (
    "You sort a mobile network customer's complaint into exactly one of the categories offered. "
    "Reply with JSON: {\"category\": <one of the offered categories>, \"reason\": <at most 15 words>}. "
    "Never repeat personal details from the complaint."
)


def llm_tiebreak(port: Any, text: str, candidates: list[str], *, model: str = "claude-opus-5") -> str | None:
    """Ask the model to choose between ``candidates``; any other answer or any failure is None."""
    from noc_agents.llm.redaction import scrub_contacts

    try:
        parsed, _record = port.draft(
            model=model,
            system=TIEBREAK_SYSTEM,
            user=f"Categories: {', '.join(candidates)}\nComplaint: {scrub_contacts(text) or ''}",
            output_model=TieBreak,
            effort="low",
            max_tokens=256,
        )
    except Exception:  # noqa: BLE001 -- a tie-break must never be why a complaint fails
        log.warning("support triage: LLM tie-break failed; keeping the rule result")
        return None
    choice = getattr(parsed, "category", None) if parsed is not None else None
    return choice if choice in candidates else None


# ------------------------------------------------------------------------------- triage


def triage(text: str, *, gazetteer: Gazetteer, policy: SupportPolicy, port: Any | None = None) -> TriageResult:
    """Classify ``text`` and choose the route. Deterministic unless ``port`` is given."""
    original = clean(text)
    norm = NEGATED_RISK.sub("notrisk", normalise(original))
    flags = risk_flags(original, norm)
    scores, fired = score_categories(norm)
    if "fraud_or_sim_swap" in flags:
        scores["sim_and_fraud"] += FRAUD_CATEGORY_WEIGHT
        fired["sim_and_fraud"].append("fraud flag")
    category, confidence = confidence_of(scores)
    matched = fired.get(category, [])
    reasons = [f"category {category}: {', '.join(matched)}" if matched else "category other: no phrase matched"]
    source = "rules"

    ranked = [c for c, s in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])) if s > 0]
    if port is not None and confidence < policy.llm_tiebreak_below and len(ranked) >= 2:
        choice = llm_tiebreak(port, original, ranked[:2])
        if choice is not None:
            category, source = choice, "llm_tiebreak"
            confidence = max(confidence, policy.low_confidence_threshold)
            reasons.append(f"LLM tie-break chose {choice} from {ranked[:2]}")

    for code, phrases in flags.items():
        reasons.append(f"risk {code}: " + ", ".join(phrases))
    amounts = extract_amounts(original)
    urgency, urgency_why = urgency_of(norm, category, flags, amounts)
    if urgency_why:
        reasons.append(f"urgency {urgency}: " + ", ".join(urgency_why))
    sentiment, sentiment_why = sentiment_of(original, norm)
    if sentiment_why:
        reasons.append(f"sentiment {sentiment}: " + ", ".join(sentiment_why))
    places = gazetteer.find(original)

    intent, tool, triggers = (None, None, []) if flags else intent_of(norm, category, places)
    if intent:
        reasons.append(f"intent {intent}: " + ", ".join(triggers))
    route = "human" if flags else ("action" if intent else "resolver")

    return TriageResult(
        category=category,
        confidence=confidence,
        urgency=urgency,
        sentiment=sentiment,
        language=detect_language(original),
        risk_flags=tuple(code for code in RISK_LEXICON if code in flags),
        intent=intent,
        tool=tool,
        route=route,
        places=tuple(places),
        scores=scores,
        reasons=reasons,
        source=source,
    )
