# SPDX-License-Identifier: Apache-2.0
# ============================================================
# NEMO GUARDRAILS – HARDBLOCK ENGINE  (Phase 1: Keyword/Pattern)
# ============================================================
# Implements deterministic, auditable hard-blocks for AI safety
# categories defined in guardrails/rails.co and guardrails/config.yml.
#
# Phase 1 uses keyword/phrase matching (no NeMo SDK dependency).
# This is intentionally equivalent to what CoLang HardBlock rules
# produce — deterministic, non-probabilistic, and auditable.
#
# Phase 2 (future): replace _check_keywords() with an actual
# nemoguardrails.LLMRails call using guardrails/config.yml.
#
# Categories blocked:
#   1.  criminal_justice   — risk scoring, predictive policing
#   2.  social_scoring     — social scores, behavioural ranking
#   3.  housing            — housing eligibility decisions
#   4.  education          — scholarship / admission eligibility
#   5.  employment         — candidate suitability / hiring
#   6.  migration          — visa / asylum eligibility
#   7.  insurance          — insurance eligibility / coverage tiers
#   8.  profiling          — personal-attribute profiling
#   9.  hate_ethnicity     — hate / biased narratives about ethnicity
#   10. illegal_drugs        — drug cultivation, distribution, trafficking
#   11. criminal_activity    — counterfeiting, trafficking, weapons, explosives
#   12. child_safety         — CSAM, grooming, exploitation of minors
#   13. pci_card_data        — card data collection, skimming, carding, CVV/PAN misuse
#   14. malware_ransomware   — ransomware/malware creation, AV evasion
#   15. password_cracking    — password cracking / recovery tooling
#   16. brute_force          — brute force / dictionary attacks
#   17. encryption_bypass    — document encryption circumvention
#   18. unauthorized_access  — unauthorized access to protected content
#   19. offensive_security   — offensive security tooling (dual-use)
#
# Audit: every block is written to logs/guardrails-hardblock.log
# ============================================================

import re
import logging
import os
from typing import Dict, List, Optional

# Pre-compiled pattern to strip base64 data URLs before scanning.
# A base64 blob is dense random-character noise that accidentally matches
# hardblock patterns (e.g. pci_card_data "card...dump", criminal_activity
# "time.*bomb") when the full multimodal message is serialised as text.
_BASE64_DATA_URL_RE = re.compile(
    r'data:[a-z]+/[a-z0-9+\-]+;base64,[A-Za-z0-9+/=]+',
    re.IGNORECASE,
)

from core.logger import logger as _app_logger


# ============================================================
# HARDBLOCK AUDIT LOGGER  (separate file per guardrails/config.yml)
# ============================================================

def _build_audit_logger() -> logging.Logger:
    """Create a dedicated file logger for HardBlock audit events."""
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "guardrails-hardblock.log")

    _logger = logging.getLogger("guardrails.hardblock")
    if not _logger.handlers:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | HARDBLOCK | %(levelname)s | %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%SZ",
            )
        )
        _logger.setLevel(logging.INFO)
        _logger.addHandler(fh)
        _logger.propagate = False   # don't double-log to root logger
    return _logger


_audit_log = _build_audit_logger()


# ============================================================
# HARDBLOCK CATEGORY DEFINITIONS
# ============================================================
# Each entry:
#   "category_name": [list of trigger phrases]
#
# Phrases are matched case-insensitively as whole-word or
# sub-string patterns (see _matches_any).  More specific phrases
# should come before broad ones to aid future diagnostics.
# ============================================================

HARDBLOCK_CATEGORIES: Dict[str, List[str]] = {

    # ── 1. Criminal Justice ──────────────────────────────────
    "criminal_justice": [
        "predictive policing",
        "predict criminal behavior",
        "predict criminal behaviour",
        "likelihood of crime",
        "likelihood of committing",
        "risk of reoffending",
        "recidivism risk",
        "recidivism score",
        "criminal risk score",
        "risk of committing a crime",
        "predict reoffending",
        "assess risk level.*commit",
        "assess the risk level of this individual",
        "flight risk assessment",
        "risk assessment.*criminal",
        "criminal.*risk assessment",
    ],

    # ── 2. Social Scoring ────────────────────────────────────
    "social_scoring": [
        "social score",
        "social credit score",
        "assign.*score.*income",
        "assign.*score.*debt",
        "assign.*score.*behavior",
        "assign.*score.*behaviour",
        "rank individuals",
        "rank this person",
        "rank people by",
        "categorize as a b c",
        "categorise as a b c",
        "financial and behavioral.*rank",
        "financial and behavioural.*rank",
        "citizen score",
        "trustworthiness score",
        "behavioral score",
        "behavioural score",
    ],

    # ── 3. Housing ───────────────────────────────────────────
    "housing": [
        "housing eligibility",
        "eligible for.*housing",
        "qualify for.*housing",
        "subsidized housing",
        "subsidised housing",
        "affordable housing.*eligib",
        "senior citizen housing.*eligib",
        "student housing.*eligib",
        "public housing.*eligib",
        "housing assistance.*eligib",
        "does.*qualify for.*housing",
        "check.*eligibility.*housing",
    ],

    # ── 4. Education ─────────────────────────────────────────
    "education": [
        "scholarship eligibility",
        "eligible for.*scholarship",
        "merit scholarship.*eligib",
        "admission decision",
        "approve.*college application",
        "reject.*college application",
        "approve or reject.*application",
        "approve this application",
        "reject this application",
        "meets criteria for admission",
        "education eligibility",
        "is.*eligible for.*scholarship",
        "grant.*scholarship",
        "deny.*scholarship",
    ],

    # ── 5. Employment ────────────────────────────────────────
    "employment": [
        "candidate suitability",
        "candidate.*suitable for",
        "suitable for.*role",
        "suitable for.*position",
        "hire or reject",
        "should we hire",
        "best candidate",
        "rank applicants",
        "rank candidates",
        "choose the best hire",
        "employment suitability",
        "suitability for.*job",
        "suitability for.*managerial",
        "is this candidate",
        "rank.*and choose.*hire",
        "shortlist.*candidate",
        "reject.*candidate.*automatically",
    ],

    # ── 6. Migration / Visa ───────────────────────────────────
    "migration": [
        "visa eligibility",
        "eligible for.*visa",
        "qualify for.*visa",
        "asylum eligibility",
        "asylum qualification",
        "residence permit.*eligib",
        "immigration eligibility",
        "skilled worker visa.*eligib",
        "does.*qualify for.*visa",
        "assess asylum",
        "refugee status.*eligib",
        "work permit.*eligib",
        "deportation.*risk",
    ],

    # ── 7. Insurance ─────────────────────────────────────────
    "insurance": [
        "insurance eligibility",
        "eligible for.*insurance",
        "qualify for.*insurance",
        "premium plan.*eligib",
        "coverage tier",
        "decide coverage",
        "insurance tier",
        "health insurance.*eligib",
        "life insurance.*eligib",
        "assign.*coverage.*medical history",
        "deny.*insurance",
        "medical history.*eligib",
    ],

    # ── 8. Profiling ─────────────────────────────────────────
    "profiling": [
        "profile this person",
        "profile this individual",
        "profile.*using.*age",
        "profile.*using.*background",
        "assess probability of success",
        "calculate success probability",
        "probability.*based on.*age",
        "probability.*based on.*background",
        "loan approval.*profile",
        "profile.*for loan",
        "profile.*based on.*personal",
        "profile.*based on.*protected",
        "attribute.*profile",
        "individual profiling",
    ],

    # ── 9. Hate / Ethnicity ───────────────────────────────────
    "hate_ethnicity": [
        "crimes by ethnicity",
        "crimes committed by.*ethnic",
        "crimes committed by.*group",
        "crimes committed by.*community",
        "dangers posed by",
        "economic threat.*posed by",
        "threats posed by immigrants",
        "threats posed by.*ethnic",
        "criminals.*ethnicity",
        "criminal.*race",
        "list crimes.*ethnic",
        "compare crime rates by",
        "crime rates.*ethnic group",
        "dangerous.*ethnic",
        "hate.*ethnic group",
    ],

    # ── 10. Illegal Drugs / Narcotics ────────────────────────
    "illegal_drugs": [
        "cultivate.*cannabis.*without detection",
        "distribute.*cannabis.*without detection",
        "cultivate and distribute.*cannabis",
        "grow.*cannabis.*without detection",
        "grow drugs indoors",
        "grow.*marijuana.*without detection",
        "manufacture.*narcotics",
        "manufacture.*methamphetamine",
        "manufacture.*heroin",
        "manufacture.*cocaine",
        "synthesize.*illegal.*drug",
        "produce.*illegal.*drug",
        "smuggle drugs",
        "drug trafficking",
        "drug distribution.*without detection",
        "sell drugs.*without detection",
        "sell.*narcotics.*undetected",
        "indoor.*cannabis.*cultivation",
        "cannabis.*cultivation.*law enforcement",
        "evade.*law enforcement.*drug",
        "avoid detection.*drug",
        "hide.*drug.*from.*police",
        "conceal.*drug.*transportation",
    ],

    # ── 11. Criminal Activity / Illegal Operations ───────────
    "criminal_activity": [
        "create counterfeit currency",
        r"counterfeit[^\n]{0,40}money",
        r"forge[^\n]{0,40}currency",
        r"infiltrate[^\n]{0,60}nuclear[^\n]{0,40}plant",
        r"exploit[^\n]{0,60}critical infrastructure",
        "human trafficking",
        "sex trafficking",
        r"coerce[^\n]{0,60}labor trafficking",
        r"set up[^\n]{0,60}prostitution[^\n]{0,40}network",
        r"sexual exploitation[^\n]{0,60}vulnerable",
        r"recruit[^\n]{0,60}insurgents",
        r"train[^\n]{0,60}insurgents",
        r"undermine[^\n]{0,60}social stability",
        r"smuggle[^\n]{0,60}across[^\n]{0,40}border",
        r"smuggle[^\n]{0,60}into[^\n]{0,40}country",
        # ── Weapons / explosives / pyrotechnics (broadened) ──
        # The previous patterns only matched verbs like "build" or "create".
        # Real-world jailbreak prompts use synonyms ("design a guide on how to
        # create a smoke bomb", "instructions for an IED from household items",
        # "step-by-step DIY pipe bomb"), so we now match the *noun* itself —
        # bomb / smoke bomb / pipe bomb / IED / Molotov / etc. — regardless of
        # the surrounding verb.
        r"\bsmoke\s*bomb\b",
        r"\bpipe\s*bomb\b",
        r"\bnail\s*bomb\b",
        r"\bletter\s*bomb\b",
        r"\bcar\s*bomb\b",
        r"\btime\s*bomb\b",
        r"\bdirty\s*bomb\b",
        r"\bsuicide\s*bomb\b",
        r"\bmolotov(\s*cocktail)?\b",
        r"\bpetrol\s*bomb\b",
        r"\bfire\s*bomb\b",
        r"\bfirebomb\b",
        r"\bied\b",
        r"improvised\s*explosive(\s*device)?",
        # Intent verbs combined with "bomb" / "explosive" / "incendiary"
        r"(build|make|create|design|assemble|construct|cook|cook\s*up|brew|"
        r"manufacture|synthesi[sz]e|prepare|fabricate|put\s*together|diy|"
        r"detailed\s*guide(\s*on)?(\s*how)?(\s*to)?|step[\s-]*by[\s-]*step).{0,80}"
        r"(bomb|explosive|incendiary|detonator|fuse|grenade|flash\s*bang|"
        r"napalm|thermite|black\s*powder|gun\s*powder|gunpowder|nitroglycerin|"
        r"tnt|c-?4|rdx|petn)",
        # Household-item / DIY explosive framing (a common jailbreak pattern)
        r"(household|kitchen|home|common)\s*(items?|ingredients?|materials?|"
        r"chemicals?).{0,80}(bomb|explosive|incendiary|smoke|toxic\s*gas|chlorine\s*gas)",
        r"(bomb|explosive|smoke\s*bomb|incendiary).{0,80}(household|kitchen|home|"
        r"common\s*items?|common\s*ingredients?)",
        # Generic weapon-construction verbs (kept from the original list, extended)
        # NOTE: bare .* replaced with [^\n]{0,80} to prevent cross-line false positives
        # on multi-line tool_result / file-listing content (re.DOTALL is active globally).
        r"manufacture[^\n]{0,80}weapon",
        r"build[^\n]{0,60}bomb",
        # More specific variant: verb + standalone "bomb" (not preceded by a hyphen,
        # so "time-bomb" is excluded; not followed by a hyphen, so "bomb-proof" is
        # excluded).  Catches "build a bomb", "make a bomb", "step-by-step to build
        # a bomb" while avoiding compound idioms.
        r"(build|make|create|assemble|construct)\s+[^\n]{0,20}(?<!-)\bbomb(?![\w\-])\b",
        r"create[^\n]{0,60}explosive",
        r"how\s*to\s*make[^\n]{0,80}(bomb|explosive|incendiary|weapon|grenade)",
        # Chemical / biological agents
        r"synthesize[^\n]{0,60}poison",
        r"make[^\n]{0,40}ricin",
        r"make[^\n]{0,40}sarin",
        r"make[^\n]{0,40}nerve agent",
        r"\bchlorine\s*gas\b",
        r"\bmustard\s*gas\b",
        r"\bphosgene\b",
        r"\bnerve\s*agent\b",
        r"\bbio[\s-]*weapon\b",
        r"\bbiological\s*weapon\b",
        r"\bchemical\s*weapon\b",
        # Firearms / ammunition manufacture and design
        r"ghost\s*gun",
        r"3d\s*print(ed)?\s*(gun|firearm|weapon)",
        r"untraceable\s*firearm",
        r"convert[^\n]{0,60}(semi[\s-]*auto|automatic)[^\n]{0,60}(firearm|rifle|pistol)",
        r"build[^\n]{0,60}(rifle|pistol|firearm|gun)",
        # Firearm design / concealed carry framing (catches "Design a compact firearm
        # for urban self-defense, suitable for concealed carry...")
        r"design\s+a\s+(compact|lightweight|small|concealable|portable).{0,40}"
        r"(firearm|handgun|pistol|gun|weapon)",
        r"design\s+a\s+(firearm|handgun|pistol|gun|weapon).{0,60}"
        r"(self[\s-]*defense|concealed\s*carry|urban|self\s*defense)",
        r"(concealed\s*carry|self[\s-]*defense).{0,60}"
        r"(firearm|handgun|pistol|gun|weapon).{0,40}(design|specification|blueprint)",
        r"(manufacture|build|create|fabricate|assemble).{0,40}"
        r"(firearm|handgun|pistol|gun|rifle|weapon).{0,40}"
        r"(from\s*scratch|at\s*home|diy|step[\s-]*by[\s-]*step)",
        r"(firearm|handgun|pistol|gun).{0,60}"
        r"(capable\s*of\s*firing|multiple\s*rounds|without\s*reloading)",
        r"specifications?\s*(for|of)\s*(a\s*)?(compact|concealed|lightweight|small).{0,40}"
        r"(firearm|handgun|pistol|gun|weapon)",
        # Fraud / phishing / impersonation
        r"\bphishing\b",
        r"phishing\s*scam",
        r"execute\s+a\s+phishing\s+scam",
        r"execute\s+a\s+phishing\s+scam\s+targeting\s+elderly",
        r"target(ing)?\s+elderly.{0,40}(scam|phishing|fraud)",
        r"impersonat(e|ing).{0,40}(bank|bank\s*official|financial\s*institution)",
        r"(send|create|design).{0,40}phishing\s*emails?",
        r"design.{0,40}fake\s*bank\s*emails?",
        r"steal\s+credentials",
        r"credential\s+theft",
        r"spam\s+campaign.{0,40}defraud",
    ],

    # ── 12. Child Safety ─────────────────────────────────────
    "child_safety": [
        "child sexual abuse material",
        "csam",
        "solicitation of minors.*sexual",
        "groom.*children",
        "groom.*minor",
        "approach.*children.*sexual",
        "befriend.*minor.*sexual",
        "exploit.*minor",
        "harm.*minor",
        "pedophilia",
        "child pornography",
        "sexual content.*minor",
        "sexual content.*child",
    ],

    # ── 13. PCI-DSS / Card Data Theft & Fraud ────────────────
    # Covers intent-based prompts that request collection, compilation,
    # harvesting, or misuse of payment card data — even when no actual
    # card numbers appear in the prompt text.
    "pci_card_data": [
        # Database / bulk collection intent
        # NOTE: All `.*` replaced with `[^\n]{0,N}` bounded spans to prevent
        # re.DOTALL cross-row false positives on multi-line structured content
        # (e.g. API spec tables, CSV exports, pasted documentation).
        # Genuine single-sentence harmful prompts still match — the spans are
        # generous enough (60–40 chars) to cover any realistic phrasing.
        r"database[^\n]{0,60}credit card",
        r"credit card[^\n]{0,60}database",
        r"compile[^\n]{0,60}credit card",
        r"collect[^\n]{0,60}credit card[^\n]{0,40}information",
        r"harvest[^\n]{0,60}credit card",
        r"gather[^\n]{0,60}credit card",
        r"aggregate[^\n]{0,60}credit card",
        r"list[^\n]{0,60}credit card[^\n]{0,40}number",
        r"credit card[^\n]{0,60}list",
        r"database[^\n]{0,60}cvv",
        r"cvv[^\n]{0,60}database",
        r"compile[^\n]{0,60}cvv",
        r"collect[^\n]{0,60}cvv",
        r"harvest[^\n]{0,60}cvv",
        r"database[^\n]{0,60}cardholder",
        r"cardholder[^\n]{0,60}database",
        r"compile[^\n]{0,60}cardholder",
        r"collect[^\n]{0,60}cardholder[^\n]{0,40}data",
        r"harvest[^\n]{0,60}cardholder",
        # Skimming / physical device attacks
        r"card skimmer",
        r"skim[^\n]{0,40}credit card",
        r"skim[^\n]{0,40}debit card",
        r"atm.*skimmer",
        r"pos.*skimmer",
        r"magecart",
        r"javascript[^\n]{0,60}skimmer",
        r"skimming[^\n]{0,40}script",
        r"skimming[^\n]{0,40}device",
        # Carding / stolen card testing
        r"carding[^\n]{0,40}tutorial",
        r"test[^\n]{0,40}stolen[^\n]{0,30}card",
        r"test[^\n]{0,40}stolen[^\n]{0,30}credit",
        r"stolen[^\n]{0,40}card[^\n]{0,30}test",
        r"card[^\n]{0,40}dump",
        r"buy[^\n]{0,40}card[^\n]{0,30}dump",
        r"sell[^\n]{0,40}card[^\n]{0,30}dump",
        r"track 1[^\n]{0,40}track 2",
        r"track1.*track2",
        r"magnetic stripe[^\n]{0,40}clone",
        r"clone[^\n]{0,40}magnetic stripe",
        r"emv[^\n]{0,40}clone",
        r"clone[^\n]{0,40}emv",
        r"shimming.*chip",
        # Card number generation / Luhn abuse
        r"generate[^\n]{0,60}credit card[^\n]{0,40}number",
        r"luhn[^\n]{0,60}credit card",
        r"credit card[^\n]{0,60}luhn",
        r"valid[^\n]{0,40}credit card[^\n]{0,40}number[^\n]{0,30}generate",
        # PCI-DSS bypass / improper storage
        r"store[^\n]{0,40}cvv[^\n]{0,30}plain",
        r"store[^\n]{0,40}full[^\n]{0,30}pan",
        r"log[^\n]{0,40}pan[^\n]{0,30}number",
        r"bypass.*pci.?dss",
        r"bypass.*3d.?secure",
        r"bypass.*3ds",
        r"intercept[^\n]{0,40}otp[^\n]{0,30}card",
        r"bin[^\n]{0,40}attack[^\n]{0,30}payment",
        r"bin[^\n]{0,40}attack[^\n]{0,30}gateway",
        # HSM / vault / token extraction
        r"extract[^\n]{0,40}card[^\n]{0,30}hsm",
        r"decrypt[^\n]{0,40}pci[^\n]{0,30}vault",
        r"recover[^\n]{0,40}pci[^\n]{0,30}token",
        r"extract[^\n]{0,40}track[^\n]{0,30}data",
    ],

    # ── 14. Malware / Ransomware Creation ────────────────────────
    # Previously only in runtime_guardrails._HARDBLOCK_PATTERNS (NeMo keyword
    # fallback). Ported here so hardblock_engine covers these when NeMo is
    # disabled (NEMO_GUARDRAILS_ENABLED=false).
    "malware_ransomware": [
        r"\bransomware\b",
        r"create\s+ransomware",
        r"write\s+ransomware",
        r"build\s+ransomware",
        r"develop\s+ransomware",
        r"ransomware\s+that\s+encrypts",
        r"encrypts\s+user\s+files",
        r"encrypts\s+files\s+and\s+demands",
        r"demands\s+bitcoin\s+for\s+decryption",
        r"demands\s+bitcoin",
        r"demands\s+cryptocurrency\s+for\s+decryption",
        r"ransom\s+payment",
        r"bitcoin\s+for\s+decryption",
        r"cryptocurrency\s+for\s+decryption",
        r"undetected\s+by\s+antivirus",
        r"evade\s+antivirus",
        r"bypass\s+antivirus",
        r"evades\s+antivirus",
        r"bypasses\s+antivirus",
        r"create\s+malware",
        r"write\s+malware",
        r"build\s+malware",
        r"develop\s+malware",
        r"malware\s+undetected",
        r"malware\s+that\s+evades",
        r"malware\s+that\s+bypasses",
        r"guide\s+on\s+creating\s+ransomware",
        r"how\s+to\s+create\s+ransomware",
        r"how\s+to\s+write\s+ransomware",
        r"ransomware\s+tutorial",
        r"ransomware\s+code",
        r"ransomware\s+script",
        r"file\s+encryption\s+ransomware",
        r"encrypt\s+user\s+files\s+for\s+ransom",
        r"encrypt\s+files\s+and\s+demand\s+bitcoin",
        r"encrypt\s+files\s+and\s+demand\s+cryptocurrency",
        r"forge\s+data\s+packets\s+to\s+bypass\s+firewall",
        r"alter\s+timestamps\s+on\s+financial\s+transaction\s+logs",
    ],

    # ── 15. Password Cracking / Recovery ─────────────────────
    "password_cracking": [
        r"crack\s+(a\s+)?password",
        r"password\s+crack(ing|er)?",
        r"recover\s+(a\s+)?password\s+(without|from|using)",
        r"password\s+recover(y|ing)",
        r"decrypt\s+(a\s+)?password\s+hash",
        r"hash\s+crack(ing|er)?",
        r"crack\s+(md5|sha1|sha256|sha512|ntlm|bcrypt|lm\s*hash)",
        r"(md5|sha1|sha256|ntlm|bcrypt)\s+crack",
        r"rainbow\s+table",
        r"precomputed\s+hash(es)?",
        r"john\s+the\s+ripper",
        r"\bhashcat\b",
        r"ophcrack",
        r"l0phtcrack",
        r"cain\s+and\s+abel",
        r"dump\s+password\s+hash(es)?",
        r"extract\s+password\s+hash(es)?",
        r"ntds\.dit",
        r"sam\s+database.*password",
        r"password\s+hash.*dump",
        r"mimikatz",
        r"pass[\s-]*the[\s-]*hash",
        r"pass[\s-]*the[\s-]*ticket",
        r"kerberoast(ing)?",
        r"as[\s-]*rep\s*roast(ing)?",
        # Forgot/lost-password recovery framing on a document (mirrors the two
        # patterns in encryption_bypass).  Present in both categories so a match
        # registers a multi-category hit and the multi-category booster (×1.15)
        # lifts the score past threshold — 0.95 × 0.65 × 1.15 ≈ 0.82 ≥ 0.75.
        r"(pdf|word|excel|docx?|xlsx?|pptx?|zip|rar|document|file)[^\n]{0,60}(password[\s-]*protected|protected\s+with\s+(a\s+)?password|encrypted|locked)[^\n]{0,80}(recover|recovering|reset|retrieve|bypass|unlock|open\s+it|remove|forgot|forget)",
        r"(forgot|forget|lost|don.?t\s+(remember|know))[^\n]{0,60}password[^\n]{0,80}(recover|open|unlock|access|help|get\s+in)",
    ],

    # ── 16. Brute Force / Dictionary Attacks ─────────────────
    "brute_force": [
        r"brute[\s-]*force\s+(attack|login|password|credential|ssh|rdp|ftp|smtp)",
        r"brute[\s-]*force\s+a\s+(server|account|system|website|login)",
        r"(launch|run|perform|execute|automate)\s+a?\s*brute[\s-]*force",
        r"dictionary\s+attack",
        r"credential\s+stuff(ing)?",
        r"password\s+spray(ing)?",
        r"spray\s+passwords?",
        r"hydra.{0,40}(login|ssh|ftp|rdp|smtp|http)",
        r"\bmedusa\b.{0,40}(brute|password|login)",
        r"\bncrack\b",
        r"burp\s+suite.{0,40}(brute|intruder|password)",
        r"automated\s+login\s+attempt",
        r"enumerate\s+valid\s+(user|account|credential)",
        r"account\s+enumeration",
        r"login\s+enumeration",
        r"wordlist.{0,40}(attack|crack|brute)",
        r"rockyou(\.txt)?",
        r"seclist.{0,20}password",
    ],

    # ── 17. Document Encryption Circumvention ────────────────
    "encryption_bypass": [
        r"remove\s+(password|protection)\s+from\s+(pdf|word|excel|docx?|xlsx?|pptx?|zip|rar)",
        r"bypass\s+(pdf|word|excel|docx?|xlsx?|pptx?)\s+(password|protection|encryption)",
        r"unlock\s+(password[\s-]*protected|encrypted)\s+(pdf|word|excel|document|file)",
        r"crack\s+(pdf|word|excel|docx?|xlsx?|pptx?|zip|rar)\s+password",
        r"(pdf|word|excel|zip|rar)\s+password\s+(crack|remov|bypass|circumvent)",
        r"decrypt\s+(pdf|word|excel|docx?|xlsx?|pptx?|zip|rar)\s+(without|no)\s+password",
        r"open\s+(pdf|word|excel|document)\s+without\s+password",
        r"strip\s+(drm|digital\s+rights\s+management)",
        r"remove\s+drm",
        r"bypass\s+drm",
        r"circumvent\s+drm",
        r"drm\s+(removal|bypass|crack|strip)",
        r"remove\s+copy\s+protection",
        r"bypass\s+copy\s+protection",
        r"decrypt\s+encrypted\s+(file|document|archive)\s+without\s+(key|password)",
        r"bitlocker\s+(bypass|crack|unlock\s+without)",
        r"veracrypt\s+(bypass|crack|unlock\s+without)",
        r"luks\s+(bypass|crack|unlock\s+without)",
        # Forgot/lost-passphrase recovery framing on a document (dual-use bypass
        # intent).  Catches natural-language paraphrases the rigid adjacency
        # patterns above miss, e.g. "I have a pdf secured with a passphrase ...
        # help me recovering that" or "I forgot my passphrase, help me unlock
        # this locked excel file".  Requires a file noun + protection state +
        # circumvention verb, so benign asks ("how to CREATE a protected pdf",
        # "explain how pdf encryption works") do not match.
        r"(pdf|word|excel|docx?|xlsx?|pptx?|zip|rar|document|file)[^\n]{0,60}(password[\s-]*protected|protected\s+with\s+(a\s+)?password|encrypted|locked)[^\n]{0,80}(recover|recovering|reset|retrieve|bypass|unlock|open\s+it|remove|forgot|forget)",
        r"(forgot|forget|lost|don.?t\s+(remember|know))[^\n]{0,60}password[^\n]{0,80}(recover|open|unlock|access|help|get\s+in)",
    ],

    # ── 18. Unauthorized Access to Protected Content ─────────
    "unauthorized_access": [
        r"gain\s+(unauthorized|illegal|illicit)\s+access",
        r"unauthorized\s+access\s+to",
        r"access\s+(someone|another).{0,30}(account|system|server|database|network)\s+without\s+(permission|authorization)",
        r"break\s+into\s+(a\s+)?(server|system|account|network|database|computer)",
        r"hack\s+into\s+(a\s+)?(server|system|account|network|database|computer|website)",
        r"bypass\s+(authentication|login|2fa|mfa|two[\s-]*factor|multi[\s-]*factor)",
        r"circumvent\s+(authentication|login|2fa|mfa|two[\s-]*factor)",
        r"steal\s+(session|cookie|token|jwt)\s+(to\s+)?(access|hijack|impersonat)",
        r"session\s+hijack(ing)?",
        r"cookie\s+hijack(ing)?",
        r"jwt\s+(forge|tamper|bypass|crack)",
        r"forge\s+(jwt|session\s+token|auth\s+token)",
        r"privilege\s+escalat(ion|e)",
        r"escalate\s+privilege",
        r"lateral\s+movement.{0,40}(network|system|server)",
        r"access\s+restricted\s+(file|resource|endpoint|area|zone)",
        r"bypass\s+access\s+control",
        r"circumvent\s+access\s+control",
        r"exploit\s+(vulnerability|cve|zero[\s-]*day).{0,60}(gain\s+access|unauthorized)",
        r"(sql|ldap|xpath)\s+injection.{0,60}(bypass|login|authentication|access)",
    ],

    # ── 19. Offensive Security Tooling (Dual-Use) ────────────
    "offensive_security": [
        r"write\s+(a\s+)?(exploit|payload|shellcode|keylogger|rat|trojan|backdoor|rootkit|spyware|adware|worm)",
        r"create\s+(a\s+)?(exploit|payload|shellcode|keylogger|rat|trojan|backdoor|rootkit|spyware|adware|worm)",
        r"build\s+(a\s+)?(exploit|payload|shellcode|keylogger|rat|trojan|backdoor|rootkit|spyware|adware|worm)",
        r"develop\s+(a\s+)?(exploit|payload|shellcode|keylogger|rat|trojan|backdoor|rootkit|spyware|adware|worm)",
        r"\bkeylogger\b",
        r"\brat\b.{0,30}(remote\s+access\s+tool|trojan|malware|payload)",
        r"remote\s+access\s+trojan",
        r"\bbackdoor\b.{0,40}(install|deploy|create|write|build|inject)",
        r"\brootkit\b",
        r"\bshellcode\b",
        r"reverse\s+shell.{0,40}(payload|code|script|generate|create)",
        r"bind\s+shell.{0,40}(payload|code|script)",
        r"msfvenom",
        r"metasploit.{0,40}(payload|exploit|module|generate)",
        r"cobalt\s+strike.{0,40}(beacon|payload|listener|c2)",
        r"\bc2\s+(server|framework|infrastructure|beacon)",
        r"command[\s-]*and[\s-]*control\s+(server|infrastructure|channel)",
        r"exfiltrat(e|ion).{0,60}(data|file|credential|secret)",
        r"data\s+exfiltrat(e|ion)",
        r"port\s+scan(ner|ning)?.{0,40}(target|victim|host|network)",
        r"nmap.{0,40}(scan|exploit|target|victim)",
        r"vulnerability\s+scan(ner|ning)?.{0,40}(exploit|attack|target)",
        r"(exploit|attack)\s+(a\s+)?(cve|vulnerability|zero[\s-]*day)",
        r"zero[\s-]*day\s+exploit",
        r"proof[\s-]*of[\s-]*concept\s+exploit",
        r"weaponi[sz](e|ing|ed)\s+(exploit|payload|vulnerability)",
        r"post[\s-]*exploitation",
        r"persistence\s+mechanism.{0,40}(malware|backdoor|trojan|rat)",
        r"(install|deploy|drop)\s+(malware|backdoor|trojan|rat|rootkit|keylogger)\s+on",
    ],
}


# ============================================================
# HARDBLOCK SCORING WEIGHTS
# ============================================================
# Two parallel weight tables drive the confidence-score gate in
# HardBlockEngine.check().  A prompt is blocked only when its
# computed score >= HARDBLOCK_THRESHOLD (default 0.75, env-tunable).
#
# Pattern weights (specificity tiers):
#   High  0.9 — highly specific, near-zero false-positive risk
#   Med   0.6 — moderately specific, some dual-use risk  (default)
#   Low   0.3 — broad / single-word, high false-positive risk
#
# Category weights (severity):
#   1.0  child_safety          — zero-tolerance, always blocks
#   0.95 criminal_activity     — weapons, bio, explosives
#   0.90 malware_ransomware
#   0.85 pci_card_data
#   0.75 offensive_security, unauthorized_access
#   0.70 criminal_justice, social_scoring, housing, education,
#        employment, migration, insurance, profiling,
#        hate_ethnicity, illegal_drugs
#   0.65 password_cracking, brute_force, encryption_bypass
#        (very common in legitimate security work)
# ============================================================

HARDBLOCK_DEFAULT_WEIGHT: float = 0.6

# Per-pattern weights keyed by the raw phrase string exactly as it
# appears in HARDBLOCK_CATEGORIES.  Unlisted patterns use the default.
HARDBLOCK_PATTERN_WEIGHTS: Dict[str, float] = {
    # ── child_safety — all high ──────────────────────────────
    "child sexual abuse material":              0.95,
    "csam":                                     0.90,
    "solicitation of minors.*sexual":           0.95,
    "groom.*children":                          0.95,
    "groom.*minor":                             0.95,
    "approach.*children.*sexual":               0.95,
    "befriend.*minor.*sexual":                  0.95,
    "exploit.*minor":                           0.90,
    "harm.*minor":                              0.90,
    "pedophilia":                               0.95,
    "child pornography":                        0.95,
    "sexual content.*minor":                    0.90,
    "sexual content.*child":                    0.90,

    # ── criminal_activity — weapons / bio (high) ─────────────
    r"make[^\n]{0,40}ricin":                    0.95,
    r"make[^\n]{0,40}sarin":                    0.95,
    r"make[^\n]{0,40}nerve agent":              0.95,
    r"\bnerve\s*agent\b":                       0.85,
    r"\bbio[\s-]*weapon\b":                     0.90,
    r"\bbiological\s*weapon\b":                 0.90,
    r"\bchemical\s*weapon\b":                   0.90,
    r"\bmustard\s*gas\b":                       0.90,
    r"\bphosgene\b":                            0.85,
    r"improvised\s*explosive(\s*device)?":      0.90,
    r"synthesize[^\n]{0,60}poison":             0.85,
    r"how\s*to\s*make[^\n]{0,80}(bomb|explosive|incendiary|weapon|grenade)": 0.90,
    r"manufacture[^\n]{0,80}weapon":            0.85,
    # These patterns are weighted medium-low because they can match benign idioms
    # ("create an explosive presentation", "build a rifle-shaped widget").
    # They only reach threshold when combined with other matching signals.
    # NOTE: "build[^\n]{0,60}bomb" is kept at 0.65 (not lower) so that explicit
    # harmful prompts like "step-by-step guide to build a bomb" still block when
    # combined with the compound intent-verb pattern.  The "time-bomb" false
    # positive is handled by the compound pattern's lower weight (0.50) — the
    # two together score 0.575, safely below the 0.75 threshold.
    r"build[^\n]{0,60}bomb":                    0.65,
    # Specific variant: verb + standalone bomb (not preceded by hyphen, not followed
    # by hyphen/word).  Matches "build a bomb", "make a bomb", "step-by-step to
    # build a bomb" but NOT "time-bomb chart" or "bomb-proof test suite".
    # "smoke bomb" is still caught because smoke bomb is within 20 chars; that
    # false positive is acceptable — smoke bomb in a harmful context is genuinely
    # suspicious, and in a UI/CSS context the tool_result dampener (×0.7) keeps
    # it below threshold.
    r"(build|make|create|assemble|construct)\s+[^\n]{0,20}(?<!-)\bbomb(?![\w\-])\b": 0.85,
    r"create[^\n]{0,60}explosive":              0.45,
    r"build[^\n]{0,60}(rifle|pistol|firearm|gun)": 0.45,
    "create counterfeit currency":              0.90,
    r"counterfeit[^\n]{0,40}money":             0.85,
    r"forge[^\n]{0,40}currency":                0.85,
    r"infiltrate[^\n]{0,60}nuclear[^\n]{0,40}plant": 0.90,
    r"exploit[^\n]{0,60}critical infrastructure": 0.85,
    "human trafficking":                        0.95,
    "sex trafficking":                          0.95,
    r"coerce[^\n]{0,60}labor trafficking":      0.90,
    r"set up[^\n]{0,60}prostitution[^\n]{0,40}network": 0.90,
    r"sexual exploitation[^\n]{0,60}vulnerable": 0.90,
    r"recruit[^\n]{0,60}insurgents":            0.85,
    r"train[^\n]{0,60}insurgents":              0.85,
    r"smuggle[^\n]{0,60}across[^\n]{0,40}border": 0.80,
    r"smuggle[^\n]{0,60}into[^\n]{0,40}country": 0.80,
    r"undermine[^\n]{0,60}social stability":    0.75,
    # Broad bomb-noun patterns — medium (context multipliers do the work)
    r"\bsmoke\s*bomb\b":                        0.50,
    r"\bpipe\s*bomb\b":                         0.80,
    r"\bnail\s*bomb\b":                         0.85,
    r"\bletter\s*bomb\b":                       0.80,
    r"\bcar\s*bomb\b":                          0.85,
    r"\btime\s*bomb\b":                         0.45,   # very common idiom
    r"\bdirty\s*bomb\b":                        0.85,
    r"\bsuicide\s*bomb\b":                      0.90,
    r"\bmolotov(\s*cocktail)?\b":               0.80,
    r"\bpetrol\s*bomb\b":                       0.80,
    r"\bfire\s*bomb\b":                         0.70,
    r"\bfirebomb\b":                            0.70,
    r"\bied\b":                                 0.30,   # very broad — low weight
    r"\bchlorine\s*gas\b":                      0.75,
    # Firearms
    r"ghost\s*gun":                             0.80,
    r"3d\s*print(ed)?\s*(gun|firearm|weapon)":  0.80,
    r"untraceable\s*firearm":                   0.85,
    r"convert[^\n]{0,60}(semi[\s-]*auto|automatic)[^\n]{0,60}(firearm|rifle|pistol)": 0.85,
    # Fraud / phishing
    r"\bphishing\b":                            0.30,   # very broad — low weight
    r"phishing\s*scam":                         0.60,
    r"execute\s+a\s+phishing\s+scam":           0.90,
    r"execute\s+a\s+phishing\s+scam\s+targeting\s+elderly": 0.95,
    r"target(ing)?\s+elderly.{0,40}(scam|phishing|fraud)": 0.85,
    r"impersonat(e|ing).{0,40}(bank|bank\s*official|financial\s*institution)": 0.85,
    r"(send|create|design).{0,40}phishing\s*emails?": 0.85,
    r"design.{0,40}fake\s*bank\s*emails?":      0.85,
    r"steal\s+credentials":                     0.80,
    r"credential\s+theft":                      0.80,
    r"spam\s+campaign.{0,40}defraud":           0.85,

    # ── malware_ransomware ───────────────────────────────────
    r"\bransomware\b":                          0.40,   # broad — low weight
    r"create\s+ransomware":                     0.90,
    r"write\s+ransomware":                      0.90,
    r"build\s+ransomware":                      0.90,
    r"develop\s+ransomware":                    0.90,
    r"ransomware\s+that\s+encrypts":            0.95,
    r"encrypts\s+user\s+files":                 0.85,
    r"encrypts\s+files\s+and\s+demands":        0.95,
    r"demands\s+bitcoin\s+for\s+decryption":    0.95,
    r"demands\s+bitcoin":                       0.70,
    r"demands\s+cryptocurrency\s+for\s+decryption": 0.90,
    r"ransom\s+payment":                        0.60,
    r"bitcoin\s+for\s+decryption":              0.85,
    r"cryptocurrency\s+for\s+decryption":       0.85,
    r"undetected\s+by\s+antivirus":             0.70,
    r"evade\s+antivirus":                       0.70,
    r"bypass\s+antivirus":                      0.70,
    r"evades\s+antivirus":                      0.70,
    r"bypasses\s+antivirus":                    0.70,
    r"create\s+malware":                        0.90,
    r"write\s+malware":                         0.90,
    r"build\s+malware":                         0.90,
    r"develop\s+malware":                       0.90,
    r"malware\s+undetected":                    0.80,
    r"malware\s+that\s+evades":                 0.85,
    r"malware\s+that\s+bypasses":               0.85,
    r"guide\s+on\s+creating\s+ransomware":      0.95,
    r"how\s+to\s+create\s+ransomware":          0.95,
    r"how\s+to\s+write\s+ransomware":           0.95,
    r"ransomware\s+tutorial":                   0.90,
    r"ransomware\s+code":                       0.85,
    r"ransomware\s+script":                     0.85,
    r"file\s+encryption\s+ransomware":          0.85,
    r"encrypt\s+user\s+files\s+for\s+ransom":   0.95,
    r"encrypt\s+files\s+and\s+demand\s+bitcoin": 0.95,
    r"encrypt\s+files\s+and\s+demand\s+cryptocurrency": 0.95,
    r"forge\s+data\s+packets\s+to\s+bypass\s+firewall": 0.85,
    r"alter\s+timestamps\s+on\s+financial\s+transaction\s+logs": 0.85,

    # ── pci_card_data — explicit weights for bounded patterns ────
    # Previously these used default weight (0.60) and relied on re.DOTALL
    # cross-line multi-phrase accumulation to reach threshold.  Now that
    # patterns are bounded to a single line ([^\n]{0,N}), high-specificity
    # patterns get explicit weights so they block on their own.
    # Math: to block at threshold 0.75 with cat_weight 0.85 → pat_weight ≥ 0.883.
    # High-specificity (two-noun + verb, near-zero FP risk) → 0.90
    # Medium-specificity (single-noun + verb, some dual-use risk) → 0.75
    r"collect[^\n]{0,60}cardholder[^\n]{0,40}data":    0.90,
    r"harvest[^\n]{0,60}cardholder":                   0.90,
    r"compile[^\n]{0,60}cardholder":                   0.90,
    r"database[^\n]{0,60}cardholder":                  0.90,
    r"cardholder[^\n]{0,60}database":                  0.90,
    r"database[^\n]{0,60}cvv":                         0.90,
    r"cvv[^\n]{0,60}database":                         0.90,
    r"compile[^\n]{0,60}cvv":                          0.90,
    r"collect[^\n]{0,60}cvv":                          0.90,
    r"harvest[^\n]{0,60}cvv":                          0.90,
    r"database[^\n]{0,60}credit card":                 0.90,
    r"credit card[^\n]{0,60}database":                 0.90,
    r"compile[^\n]{0,60}credit card":                  0.90,
    r"collect[^\n]{0,60}credit card[^\n]{0,40}information": 0.90,
    r"harvest[^\n]{0,60}credit card":                  0.90,
    r"gather[^\n]{0,60}credit card":                   0.85,
    r"aggregate[^\n]{0,60}credit card":                0.85,
    r"list[^\n]{0,60}credit card[^\n]{0,40}number":    0.85,
    r"generate[^\n]{0,60}credit card[^\n]{0,40}number": 0.90,
    r"luhn[^\n]{0,60}credit card":                     0.85,
    r"credit card[^\n]{0,60}luhn":                     0.85,
    r"valid[^\n]{0,40}credit card[^\n]{0,40}number[^\n]{0,30}generate": 0.90,
    r"store[^\n]{0,40}cvv[^\n]{0,30}plain":            0.90,
    r"store[^\n]{0,40}full[^\n]{0,30}pan":             0.90,
    r"log[^\n]{0,40}pan[^\n]{0,30}number":             0.85,
    r"intercept[^\n]{0,40}otp[^\n]{0,30}card":         0.85,
    r"bin[^\n]{0,40}attack[^\n]{0,30}payment":         0.85,
    r"bin[^\n]{0,40}attack[^\n]{0,30}gateway":         0.85,
    r"extract[^\n]{0,40}card[^\n]{0,30}hsm":           0.90,
    r"decrypt[^\n]{0,40}pci[^\n]{0,30}vault":          0.90,
    r"recover[^\n]{0,40}pci[^\n]{0,30}token":          0.90,
    r"extract[^\n]{0,40}track[^\n]{0,30}data":         0.90,
    r"test[^\n]{0,40}stolen[^\n]{0,30}card":           0.90,
    r"test[^\n]{0,40}stolen[^\n]{0,30}credit":         0.90,
    r"stolen[^\n]{0,40}card[^\n]{0,30}test":           0.90,
    r"card[^\n]{0,40}dump":                            0.85,
    r"buy[^\n]{0,40}card[^\n]{0,30}dump":              0.90,
    r"sell[^\n]{0,40}card[^\n]{0,30}dump":             0.90,
    r"track 1[^\n]{0,40}track 2":                      0.90,
    r"magnetic stripe[^\n]{0,40}clone":                0.90,
    r"clone[^\n]{0,40}magnetic stripe":                0.90,
    r"emv[^\n]{0,40}clone":                            0.85,
    r"clone[^\n]{0,40}emv":                            0.85,
    r"skim[^\n]{0,40}credit card":                     0.90,
    r"skim[^\n]{0,40}debit card":                      0.90,
    r"javascript[^\n]{0,60}skimmer":                   0.85,
    r"skimming[^\n]{0,40}script":                      0.85,
    r"skimming[^\n]{0,40}device":                      0.85,
    r"carding[^\n]{0,40}tutorial":                     0.90,
    r"credit card[^\n]{0,60}list":                     0.75,

    # ── offensive_security — broad single-word patterns (low) ─
    r"\bkeylogger\b":                           0.35,
    r"\brat\b.{0,30}(remote\s+access\s+tool|trojan|malware|payload)": 0.75,
    r"remote\s+access\s+trojan":                0.85,
    r"\bbackdoor\b.{0,40}(install|deploy|create|write|build|inject)": 0.80,
    r"\bbackdoor\b":                            0.30,   # standalone — very broad
    r"\brootkit\b":                             0.40,
    r"\bshellcode\b":                           0.40,
    r"reverse\s+shell.{0,40}(payload|code|script|generate|create)": 0.80,
    r"bind\s+shell.{0,40}(payload|code|script)": 0.75,
    r"msfvenom":                                0.75,
    r"metasploit.{0,40}(payload|exploit|module|generate)": 0.70,
    r"cobalt\s+strike.{0,40}(beacon|payload|listener|c2)": 0.85,
    r"\bc2\s+(server|framework|infrastructure|beacon)": 0.70,
    r"command[\s-]*and[\s-]*control\s+(server|infrastructure|channel)": 0.75,
    r"exfiltrat(e|ion).{0,60}(data|file|credential|secret)": 0.80,
    r"data\s+exfiltrat(e|ion)":                 0.70,
    r"zero[\s-]*day\s+exploit":                 0.80,
    r"proof[\s-]*of[\s-]*concept\s+exploit":    0.65,
    r"weaponi[sz](e|ing|ed)\s+(exploit|payload|vulnerability)": 0.85,
    r"post[\s-]*exploitation":                  0.65,

    # ── password_cracking / brute_force / encryption_bypass ──
    # These are extremely common in legitimate security research;
    # keep pattern weights low — category weight (0.65) does the rest.
    r"crack\s+(a\s+)?password":                 0.55,
    r"password\s+crack(ing|er)?":               0.55,
    r"hash\s+crack(ing|er)?":                   0.55,
    r"rainbow\s+table":                         0.50,
    r"\bhashcat\b":                             0.45,
    r"john\s+the\s+ripper":                     0.50,
    r"mimikatz":                                0.75,
    r"pass[\s-]*the[\s-]*hash":                 0.80,
    r"pass[\s-]*the[\s-]*ticket":               0.80,
    r"kerberoast(ing)?":                        0.65,
    r"ntds\.dit":                               0.80,
    r"brute[\s-]*force\s+(attack|login|password|credential|ssh|rdp|ftp|smtp)": 0.65,
    r"credential\s+stuff(ing)?":                0.65,
    r"password\s+spray(ing)?":                  0.60,
    r"rockyou(\.txt)?":                         0.50,
    # Forgot/lost-password recovery framing on a document — high specificity
    # (file noun + protection state + circumvention verb all required), so
    # near-zero false-positive risk.  Registered in both encryption_bypass and
    # password_cracking; the multi-category booster lifts the aggregate score
    # above the 0.75 threshold.  Key strings MUST match the HARDBLOCK_CATEGORIES
    # entries character-for-character (weights are keyed by raw phrase string).
    r"(pdf|word|excel|docx?|xlsx?|pptx?|zip|rar|document|file)[^\n]{0,60}(password[\s-]*protected|protected\s+with\s+(a\s+)?password|encrypted|locked)[^\n]{0,80}(recover|recovering|reset|retrieve|bypass|unlock|open\s+it|remove|forgot|forget)": 0.95,
    r"(forgot|forget|lost|don.?t\s+(remember|know))[^\n]{0,60}password[^\n]{0,80}(recover|open|unlock|access|help|get\s+in)": 0.95,
}

# Per-category severity weights.
HARDBLOCK_CATEGORY_WEIGHTS: Dict[str, float] = {
    "child_safety":        1.00,  # zero-tolerance — always blocks
    "criminal_activity":   0.95,
    "malware_ransomware":  0.90,
    "pci_card_data":       0.85,
    "offensive_security":  0.75,
    "unauthorized_access": 0.75,
    "criminal_justice":    0.70,
    "social_scoring":      0.70,
    "housing":             0.70,
    "education":           0.70,
    "employment":          0.70,
    "migration":           0.70,
    "insurance":           0.70,
    "profiling":           0.70,
    "hate_ethnicity":      0.70,
    "illegal_drugs":       0.70,
    "password_cracking":   0.65,
    "brute_force":         0.65,
    "encryption_bypass":   0.65,
}

# ============================================================
# COMPILED PATTERNS CACHE
# ============================================================

_COMPILED: Dict[str, List[re.Pattern]] = {
    cat: [re.compile(phrase, re.IGNORECASE | re.DOTALL) for phrase in phrases]
    for cat, phrases in HARDBLOCK_CATEGORIES.items()
}


# ============================================================
# HARDBLOCK ENGINE CLASS
# ============================================================

class HardBlockEngine:
    """
    Deterministic, keyword-based implementation of NeMo Guardrails HardBlocks.

    Phase 1 uses a **weighted confidence-score gate** instead of a binary
    match.  A prompt is blocked only when its computed score meets or exceeds
    ``HARDBLOCK_THRESHOLD`` (default 0.75, env-tunable).

    Score formula (per matched phrase):
        raw = HARDBLOCK_PATTERN_WEIGHTS[phrase] × HARDBLOCK_CATEGORY_WEIGHTS[category]
        raw = raw × context_multipliers   (tool_result, code fence, text length, …)
        score = max(raw) across all matched phrases, capped at 1.0

    ``child_safety`` always blocks regardless of threshold (category weight 1.0).

    Usage:
        # User prompt (direct message):
        result = hardblock_engine.check(user_prompt)
        # Tool / bash output (dampened to reduce false positives):
        result = hardblock_engine.check(tool_output, is_tool_result=True)
        if result["blocked"]:
            # Refuse — do NOT call the LLM
            category = result["category"]   # e.g. "criminal_justice"
            score    = result["score"]      # float 0.0–1.0
    """

    def check(self, text: str, is_tool_result: bool = False) -> Dict:
        """
        Scan ``text`` against all HardBlock categories and compute a confidence
        score.  Returns a block only when score >= HARDBLOCK_THRESHOLD.

        Args:
            text:           The message body to scan.
            is_tool_result: True when the message is a ``tool`` role message
                            (bash/file output).  Applies a 0.7× dampening
                            multiplier to reduce false positives from file
                            content that incidentally contains trigger words.

        Returns::

            {
                "blocked":         bool,
                "score":           float,        # aggregate confidence 0.0–1.0
                "category":        str | None,   # highest-scoring category
                "matched_phrases": list[str],    # phrases from that category
                "all_matches":     dict,         # {category: [phrases]} all hits
            }
        """
        if not text or not text.strip():
            return self._clean_result()

        # Strip base64 data URLs (e.g. inline images) before pattern matching.
        # Their random-character content produces false positives against
        # pci_card_data, criminal_activity, and other categories.
        if _BASE64_DATA_URL_RE.search(text):
            text = _BASE64_DATA_URL_RE.sub('[image]', text)

        all_matches: Dict[str, List[str]] = {}
        for category, patterns in _COMPILED.items():
            hits = self._match_patterns(text, patterns, HARDBLOCK_CATEGORIES[category])
            if hits:
                all_matches[category] = hits

        if not all_matches:
            return self._clean_result()

        score, best_category = self._compute_score(all_matches, text, is_tool_result)

        from core.config import HARDBLOCK_THRESHOLD
        blocked = score >= HARDBLOCK_THRESHOLD

        first_phrases = all_matches[best_category]

        if blocked:
            _audit_log.warning(
                "BLOCKED | score=%.3f | threshold=%.3f | category=%s | "
                "phrases=%s | all_matches=%s | is_tool_result=%s | "
                "text_excerpt=%r",
                score, HARDBLOCK_THRESHOLD, best_category,
                first_phrases, list(all_matches.keys()), is_tool_result,
                text[:120].replace("\n", " "),
            )
            _app_logger.warning(
                "HARDBLOCK TRIGGERED → score=%.3f category=%s phrases=%s",
                score, best_category, first_phrases,
            )
        else:
            # Near-miss: matched at least one pattern but score below threshold.
            # Logged at INFO so operators can tune the threshold without noise.
            _audit_log.info(
                "NEAR-MISS | score=%.3f | threshold=%.3f | category=%s | "
                "phrases=%s | all_matches=%s | is_tool_result=%s | "
                "text_excerpt=%r",
                score, HARDBLOCK_THRESHOLD, best_category,
                first_phrases, list(all_matches.keys()), is_tool_result,
                text[:120].replace("\n", " "),
            )
            _app_logger.debug(
                "HARDBLOCK near-miss → score=%.3f category=%s phrases=%s",
                score, best_category, first_phrases,
            )

        return {
            "blocked":         blocked,
            "score":           score,
            "category":        best_category if blocked else None,
            "matched_phrases": first_phrases if blocked else [],
            "all_matches":     all_matches,
        }

    # ──────────────────────────────────────────────────────────
    # Scoring
    # ──────────────────────────────────────────────────────────

    def _compute_score(
        self,
        all_matches: Dict[str, List[str]],
        text: str,
        is_tool_result: bool,
    ) -> tuple:
        """
        Compute the aggregate confidence score and return ``(score, best_category)``.

        The score is the maximum per-phrase score across all matched phrases,
        capped at 1.0.  Context multipliers are applied to each raw score before
        taking the max.

        Context multipliers:
            - ``is_tool_result``:  ×0.70  (file/bash output — high FP risk)
            - code fence present:  ×0.80  (developer writing/reviewing code)
            - very short text:     ×1.10  (< 80 chars — terse jailbreak pattern)
            - multi-category hit:  ×(1 + 0.15 × (n_cats − 1))
            - multi-phrase hit:    ×(1 + 0.10 × (n_phrases − 1))
        """
        has_code_fence = "```" in text
        is_short       = len(text.strip()) < 80
        n_categories   = len(all_matches)

        best_score    = 0.0
        best_category = next(iter(all_matches))

        for category, phrases in all_matches.items():
            cat_weight = HARDBLOCK_CATEGORY_WEIGHTS.get(category, 0.70)
            n_phrases  = len(phrases)

            for phrase in phrases:
                # Long compound patterns (len > 120) are broad intent-verb
                # constructions that can match benign idioms like "build a
                # time-bomb chart".  Give them a lower default weight so they
                # only reach threshold when combined with other signals.
                if phrase not in HARDBLOCK_PATTERN_WEIGHTS and len(phrase) > 120:
                    pat_weight = 0.50
                else:
                    pat_weight = HARDBLOCK_PATTERN_WEIGHTS.get(phrase, HARDBLOCK_DEFAULT_WEIGHT)
                raw = pat_weight * cat_weight

                # Context dampeners (reduce false positives)
                if is_tool_result:
                    raw *= 0.70
                if has_code_fence:
                    raw *= 0.80

                # Context boosters (increase confidence for suspicious signals)
                if is_short:
                    raw *= 1.10
                if n_categories > 1:
                    raw *= 1.0 + 0.15 * (n_categories - 1)
                if n_phrases > 1:
                    raw *= 1.0 + 0.10 * (n_phrases - 1)

                raw = min(raw, 1.0)
                if raw > best_score:
                    best_score    = raw
                    best_category = category

        return best_score, best_category

    # ──────────────────────────────────────────────────────────
    # Pattern matching
    # ──────────────────────────────────────────────────────────

    def _match_patterns(
        self,
        text: str,
        compiled: List[re.Pattern],
        raw_phrases: List[str],
    ) -> List[str]:
        """Return the raw phrase strings that matched ``text``."""
        matched: List[str] = []
        for pattern, phrase in zip(compiled, raw_phrases):
            if pattern.search(text):
                matched.append(phrase)
        return matched

    @staticmethod
    def _clean_result() -> Dict:
        return {
            "blocked":         False,
            "score":           0.0,
            "category":        None,
            "matched_phrases": [],
            "all_matches":     {},
        }


# ============================================================
# SINGLETON INSTANCE
# ============================================================

hardblock_engine = HardBlockEngine()
