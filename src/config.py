import os
from urllib.parse import quote_plus

# ---------------------------------------------------------------------------
# 🤖 제미나이 모델명 빈칸 자동 방어 로직
#    ✅ 2순위 반영: gemini-1.5-* 는 전부 셧다운(404). 별칭 'gemini-flash-latest'
#       사용 → 구글이 최신 flash(현재 3.5)로 자동 라우팅, 재하드코딩 불필요.
# ---------------------------------------------------------------------------
# 이미 종료된 모델(1.0/1.5/2.0 계열)은 env에 들어와 있어도 방어 → 살아있는 기본값으로 교체.
# 2.5-flash 는 2026-10-16 종료 예정이므로 그 전 교체 필요(대안: gemini-flash-latest / 3.5).
_DEAD_MODEL_PREFIXES = ("gemini-1.0", "gemini-1.5", "gemini-2.0")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()
if not GEMINI_MODEL or GEMINI_MODEL.startswith(_DEAD_MODEL_PREFIXES):
    GEMINI_MODEL = "gemini-flash-latest"
os.environ["GEMINI_MODEL"] = GEMINI_MODEL

# ---------------------------------------------------------------------------
# 1. 관심 키워드 (🚀 최신 AI 모델명, VC 펀딩 단계, 매크로 지표 완벽 확장!)
# ---------------------------------------------------------------------------
IMPACT_THEME_KEYWORDS = {
    "climate_energy": [
        "impact investing", "climate tech", "carbon neutral", "net zero",
        "energy transition", "renewable energy", "green fund", "climate fund",
        "cleantech", "decarbonization", "ev infrastructure", "climate adaptation",
        "climate resilience", "just transition", "임팩트투자", "기후테크",
        "carbon market", "carbon markets", "voluntary carbon market",
        "emissions trading",
        "탄소중립", "넷제로", "에너지전환", "재생에너지", "녹색기금",
        "기후펀드", "그린뉴딜", "클린테크", "기후적응", "기후회복력", "공정전환",
        "탄소시장", "자발적 탄소시장", "배출권시장", "탄소크레딧", "탄소배출권",
        "청정전력", "재생전력", "지열발전",
        "차세대 지열", "전력망 연계",
    ],
    "circular_nature_food": [
        "circular economy", "biodiversity", "natural capital", "nature-based solutions",
        "water access", "clean water", "food security", "sustainable agriculture",
        "waste reduction", "순환경제", "생물다양성", "자연자본", "수자원",
        "식량안보", "지속가능농업", "폐기물 감축", "이차전지",
    ],
    "care_health": [
        "care economy", "elder care", "long-term care", "healthcare", "digital health",
        "healthtech", "health equity", "healthcare access", "affordable healthcare",
        "mental health", "돌봄", "돌봄경제", "노인돌봄", "장기요양", "헬스케어",
        "디지털헬스", "헬스테크", "건강형평성", "의료접근성", "정신건강", "사회서비스",
    ],
    "education_access": [
        "education", "education access", "education equity", "learning outcomes", "edtech",
        "digital accessibility", "disability inclusion", "교육격차", "교육접근성",
        "교육", "학습성과", "에듀테크", "디지털 접근성", "장애인 접근성",
    ],
    "social_inclusion": [
        "social economy", "social enterprise", "social venture", "financial inclusion",
        "inclusive finance", "affordable housing", "workforce development", "quality jobs",
        "esg", "sustainability", "사회적경제", "사회적기업", "소셜벤처",
        "금융포용", "포용금융", "주거복지", "직업역량", "좋은 일자리", "지속가능",
    ],
}
IMPACT_KW = sorted({
    keyword
    for keywords in IMPACT_THEME_KEYWORDS.values()
    for keyword in keywords
})

AI_KW = [
    "artificial intelligence", "generative ai", "llm", "large language model",
    "gpt", "gpt-4o", "gpt-5", "o3", "o4-mini", "claude", "claude sonnet", "claude opus",
    "gemini", "gemini 2.0", "gemini 2.5", "openai", "anthropic", "deepmind",
    "grok", "deepseek", "mistral", "qwen", "kimi", "cursor", "windsurf", "codex",
    "ai startup", "ai investment", "ai fund", "ai chip", "gpu", "semiconductor",
    "ai agent", "autonomous agent", "copilot", "multimodal", "data center",
    "robotics", "robot", "humanoid", "foundation model",
    "인공지능", "생성형", "거대언어모델", "에이전트", "ai 반도체", "엔비디아", "데이터센터",
    "로보틱스", "로봇", "휴머노이드", "파운데이션 모델",
]

ALT_KW = [
    "private equity", "venture capital", "private debt", "private credit",
    "infrastructure fund", "real estate fund", "secondary fund", "buyout",
    "growth equity", "fund of funds", "lp", "gp", "dry powder",
    "series a", "series b", "series c", "series d", "series e",
    "seed round", "pre-seed", "growth round", "bridge round", "late stage",
    "fundraising", "vc funding", "unicorn", "ipo", "pre-ipo", "valuation",
    "exit", "take private", "continuation fund", "buy and build",
    "대체투자", "사모펀드", "벤처캐피탈", "사모채권", "인프라펀드",
    "부동산펀드", "세컨더리", "바이아웃", "그로스에쿼티", "출자자",
    "운용사", "드라이파우더", "블라인드펀드", "모태펀드", "공제회",
    "스타트업", "투자유치", "펀딩", "시리즈a", "시리즈b", "프리ipo", "인수", "합병", "유니콘",
    "기업가치", "밸류에이션", "구주 거래", "투자 회수", "엑시트",
]

MACRO_KW = [
    "fomc", "federal reserve", "interest rate", "inflation", "cpi",
    "gdp", "recession", "soft landing", "geopolitics", "tariff",
    "trade war", "supply chain", "sanctions", "oil price", "opec",
    "treasury yield", "10-year treasury", "yield curve", "dot plot",
    "기준금리", "인플레이션", "물가상승", "경기침체", "연준", "한국은행",
    "지정학", "관세", "무역전쟁", "공급망", "제재", "유가", "환율",
    "미중갈등", "중동", "우크라이나", "대만", "통화정책", "금리", "스콧베센트", "트럼", "거시경제"
]

INSIGHTS_KW = [
    "mckinsey", "맥킨지", "bcg", "bain", "베인", "deloitte", "딜로이트",
    "pwc", "ey", "kpmg", "sloan", "harvard business", "hbr", "insights",
    "strategy+business", "executive", "ceo survey", "megatrend", "메가트렌드",
    "whitepaper", "outlook", "survey", "report", "strategic framework",
    "보고서", "전망", "조사", "컨설팅", "트렌드", "인사이트", "산업동향"
]

INTEREST_KEYWORDS = sorted(set(IMPACT_KW + AI_KW + ALT_KW + MACRO_KW + INSIGHTS_KW))

# ✅ 2순위 반영: hackernews.py 는 keywords[:4] 만 사용하므로 INTEREST_KEYWORDS(정렬)를
#    그대로 쓰면 알파벳 앞 4개(무의미)만 검색됨 → HN용 소수 핵심어를 따로 둔다.
HN_KEYWORDS = [
    "AI", "venture capital", "climate tech", "startup",
    "OpenAI", "LLM", "energy transition", "semiconductor",
]

# ---------------------------------------------------------------------------
# 2. 카테고리 및 발송/점수 설정
# ---------------------------------------------------------------------------
CATEGORIES = {
    "🌱 임팩트": IMPACT_KW,
    "🤖 AI": AI_KW,
    "📈 대체투자": ALT_KW,
    "🌐 거시·정책·지정학": MACRO_KW,
    "👔 MBB·Big4 인사이트": INSIGHTS_KW,
}

MAX_PER_CATEGORY_DICT = {
    "🌱 임팩트": 3,
    "🤖 AI": 3,
    "📈 대체투자": 3,
    "🌐 거시·정책·지정학": 3,
    "👔 MBB·Big4 인사이트": 3,
}
MAX_PER_CATEGORY = 3
IMPACT_MUST_READ_MAX = 5
ALTERNATIVE_MAJOR_DEAL_MAX = 6
LLM_CANDIDATES_PER_CATEGORY = 12
IMPACT_CANDIDATES_PER_THEME = 3

OVERSEAS_PREFERRED_DOMAINS = ["🌱 임팩트", "🤖 AI", "📈 대체투자", "👔 MBB·Big4 인사이트"]
REGION_WEIGHT = {"global": 1.35, "korea": 1.0}
LLM_SEND_MIN_SCORE = 0

# MBB·Big4는 해외 원문을 우선하되, 국내 공식 인사이트가 마지막 해외 기사와
# 이 점수 차이 안에 있으면 국내 자료 1건을 포함할 수 있다.
INSIGHTS_DOMESTIC_SCORE_TOLERANCE = 1.0

# 기사 자격은 유지하지만 상대 순위만 낮출 항목. 값은 최종 선정 점수에 더한다.
SELECTION_SCORE_ADJUSTMENTS = {
    "branded_roundup": -1.0,
    "corporate_operating_macro": -2.0,
}

# 동일 사건을 여러 매체가 보도했을 때 대표 기사로 우선할 원 보도 출처.
# Google News 보완 피드로 수집돼도 이 값은 유지된다.
ORIGINAL_NEWSWIRE_PRIORITY = {
    "reuters": 6,
}

# ── 운영 노브(팀 운영자가 조정하는 값) ──
SLACK_MAX_LENGTH = 3900     # 슬랙 자동분할(~4000자) 방지 상한
SLACK_HEADER = ""           # 예: "📰 *ISQ Daily News | {date}*"  (빈 문자열이면 헤더 없음)
MIN_CATEGORY_NEWS = 0       # 최소 개수 없음: 부족분을 낮은 품질 기사로 강제 보충하지 않음
TRANSLATE_TITLES = True     # 발송 기사 제목 한글 번역(영문만, 실패 시 원문 유지)

SIMILARITY_THRESHOLD = 0.72
# 선정 단계에서 '같은 사건' 을 걸러낼 때 쓰는 임계값. 수집 단계보다 낮다.
# 잘못 걸러도 비슷한 기사 하나를 덜 보여줄 뿐이지만, 놓치면 한 카테고리가
# 같은 사건으로 채워진다(실제로 거시 3칸이 전부 같은 FOMC 의사록이었다).
SELECTION_SIMILARITY_THRESHOLD = 0.60
WATCHLIST_WEIGHT = 2.5       # 관심기업 존재감 극대화

# VC editor signals: market-moving events receive a higher score than routine
# coverage. The processor uses these settings so editorial policy stays here.
EDITORIAL_PRIORITY_SIGNALS = {
    "investment_or_ma": [
        "funding", "fundraise", "raises", "raised", "investment", "acquisition",
        "merger", "m&a", "buyout", "ipo", "series a", "series b", "series c",
        "투자", "투자유치", "투자 유치", "인수", "인수합병", "합병",
        "기업공개", "상장", "시리즈a", "시리즈b", "시리즈c",
    ],
    "policy_or_regulation": [
        "regulation", "regulatory", "policy", "legislation", "tariff", "sanction",
        "antitrust", "federal reserve", "interest rate",
        "규제", "정책", "법안", "법률", "관세", "제재", "무효화",
        "인증 취소", "크레딧 취소", "감축실적 보충", "당국 명령",
    ],
    "market_or_industry_shift": [
        "outlook", "forecast", "report", "global trends", "industry trends",
        "market share", "supply chain", "restructuring",
        "시장 재편", "산업 재편", "기업가치", "밸류에이션", "구주 거래",
        "투자 회수", "회수 전략", "엑시트", "가치 하락",
    ],
    "major_contract_or_technology": [
        "contract", "partnership", "agreement", "launches", "breakthrough",
        "commercial deployment", "data center",
        "대형 계약", "공급계약", "전력구매계약", "상용화", "상용 가동",
        "실증 성공", "기술 검증", "전력망 연계", "가동 성공",
    ],
    "enterprise_risk": [
        "fraud", "embezzlement", "lawsuit", "bankruptcy", "insolvency",
        "layoffs", "strike", "data breach", "contract termination",
        "배임", "횡령", "소송", "파산", "부도", "구조조정", "대규모 해고",
        "파업", "개인정보 유출", "계약 해지", "투자 철회",
        "인증 취소", "무효화", "회수 지연", "상장 불확실성", "ipo 불확실성",
    ],
    "impact_evidence": [
        "emissions reduction", "verified impact", "health outcomes", "learning outcomes",
        "public procurement", "clinical validation", "impact measurement",
        "탄소 감축", "임팩트 측정", "의료접근성 개선", "학습성과", "공공조달",
        "임상 검증", "실증 결과", "실증 성공", "전력망 연계", "상용 가동",
        "감축실적", "탄소크레딧 취소", "크레딧 무효화",
    ],
}
EDITORIAL_PRIORITY_WEIGHT = 2.0
SOFT_PENALTY_KEYWORDS = [
    "특징주", "목표가", "상한가", "하한가", "종목추천", "리딩", "주가전망"
]

# ---------------------------------------------------------------------------
# 3. 제외 정책 (명백한 비기사·소비성 콘텐츠·지원사업 신청 안내 차단)
# ---------------------------------------------------------------------------
# ✅ 비-뉴스(채용공고·행사·수상·부고 등) 하드 차단 — is_relevant 에서 사용
HARD_EXCLUSION_KEYWORDS = [
    # 채용 페이지/지원 안내. 단순한 "채용", "공고", "모집"은 기업 확장이나
    # 정책 공고까지 지울 수 있으므로 단독 키워드로 사용하지 않는다.
    "we're hiring", "we are hiring", "now hiring", "job opening", "job opportunity",
    "apply now", "join our team", "career opportunity", "is hiring", "vacancy",
    "careers at", "view all jobs", "open position", "open roles", "job description",
    "job requirements", "application deadline", "submit your application",
    "equal opportunity employer", "work with us", "internship opportunity",
    "채용 공고", "채용공고", "채용 안내", "채용안내", "채용 중", "채용중",
    "공개채용", "상시채용", "입사 지원", "입사지원", "지원 자격", "지원자격",
    "접수 기간", "접수기간", "서류 접수", "신입사원 모집", "경력사원 모집",
    "인턴 모집", "구인 공고", "기자프로필", "기자 프로필", "reporter profile",
    "author profile",
    # 행사 참가/접수 안내
    "register now", "registration open", "early bird registration", "rsvp",
    "save the date", "conference invite", "call for speakers", "call for papers",
    "ticket sales", "visit our booth", "meet us at",
    "행사 안내", "참가 신청", "참가신청", "사전 등록", "사전등록", "등록 마감",
    "참가자 모집", "연사 모집", "부스 참가", "공모전 접수", "설명회 신청",
    # 수상/회사 내부 홍보
    "award nominations open", "award ceremony", "named a winner", "wins award",
    "best workplace", "top employer", "employee spotlight", "meet the team",
    "welcome to the team", "수상자 발표", "시상식", "수상 소식", "우수기업 선정",
    "표창 수상", "임직원 소개", "직원 인터뷰",
    # 광고/판매 유도
    "sponsored content", "paid partnership", "partner content", "advertorial",
    "brand studio", "promoted content", "limited-time offer", "giveaway", "shop now",
    "협찬 콘텐츠", "유료 광고", "유료광고", "광고성 기사", "할인 코드", "할인코드",
    "경품 이벤트",
    # 부고/인사
    "obituary", "부고", "인사발령", "동정",
]

OPINION_FORMAT_KEYWORDS = [
    "opinion", "op-ed", "editorial", "guest essay", "commentary", "viewpoint",
    "columnist", "사설", "오피니언", "기고", "기고문", "칼럼", "시론", "논단",
    "기자수첩", "데스크칼럼", "논설", "논설실",
]
OPINION_URL_PATTERNS = ["/opinion/", "/editorial/", "/column/", "/commentary/"]

# 중요 사건이면 다른 신뢰도 높은 보도를 찾을 수 있도록 구제 검토하는 형식.
# 다음 bot.py 단계에서 제목·URL·설명을 구분해 적용한다.
SOFT_EDITORIAL_EXCLUSION_KEYWORDS = [
    "interview", "podcast", "webinar", "sponsored", "press release",
    "product announcement", "awards", "event registration",
]

RESCUE_EVENT_SIGNALS = [
    "acquisition", "merger", "buyout", "ipo", "series b", "series c", "series d",
    "growth round", "fund close", "private credit", "project finance",
    "regulation", "legislation", "government contract", "public procurement",
    "bankruptcy", "fraud", "data breach", "인수", "합병", "상장", "시리즈b",
    "시리즈c", "시리즈d", "펀드 결성", "사모대출", "프로젝트 파이낸싱",
    "규제", "법안", "정부 계약", "공공조달", "파산", "횡령", "개인정보 유출",
]

EDITORIAL_EXCLUSION_KEYWORDS = HARD_EXCLUSION_KEYWORDS + SOFT_EDITORIAL_EXCLUSION_KEYWORDS

# Alternative-investment deal policy. Amount is a signal, never an automatic
# cutoff: strategic, undisclosed, and impact-sector early-stage deals remain
# eligible when they satisfy the exception signals below.
DEAL_PRIORITY_SIGNALS = {
    "transaction": [
        "acquisition", "acquires", "acquired", "merger", "buyout", "take-private",
        "ipo", "secondary sale", "stake sale", "continuation fund",
        "인수", "인수합병", "합병", "바이아웃", "기업공개", "상장",
        "구주 매각", "지분 매각", "세컨더리 거래",
    ],
    "financing": [
        "series b", "series c", "series d", "growth round", "bridge financing",
        "follow-on", "private credit", "credit facility", "project finance",
        "infrastructure financing", "fund close", "fundraising",
        "시리즈b", "시리즈c", "시리즈d", "성장투자", "브릿지 투자",
        "사모대출", "프로젝트 파이낸싱", "인프라 금융", "펀드 결성", "투자유치",
    ],
    "exception": [
        "strategic investment", "undisclosed", "government contract", "public procurement",
        "valuation", "unicorn", "commercial deployment",
        "전략적 투자", "비공개", "정부 계약", "공공조달", "기업가치",
        "유니콘", "상용화",
    ],
}
DEAL_EARLY_STAGE_SIGNALS = ["seed", "pre-seed", "series a"]
DEAL_EXCLUSION_KEYWORDS = [
    "investment seminar", "investment briefing", "startup support program",
    "small business support", "grant application", "pitch competition",
]
IMPACT_EARLY_STAGE_SIGNALS = [
    "climate", "healthcare", "care economy", "education access", "circular economy",
    "social venture", "financial inclusion", "기후", "돌봄", "의료접근성",
    "교육격차", "순환경제", "소셜벤처", "금융포용",
]

BLACKLIST_KEYWORDS = [
    "coupon", "promo code", "discount code", "% off", "best deals",
    "best price", "buy now", "airdoctor", "booking.com", "best laptop",
    "laptop review", "celebrity", "sports", "entertainment", "gaming",
    "movie", "tv show", "gossip", "github repo", "code walkthrough",
    "price target", "stock forecast", "stock prediction", "analyst rating",
    "buy rating", "sell rating", "stock to buy", "why shares are up",
    "why shares are down", "why stock is up", "why stock is down",
    "technical analysis", "dividend stock", "crypto price prediction", "airdrop",
    "hands-on review", "buying guide", "gift guide", "where to buy",
    "중기자금", "소상공인", "지역화폐", "테크노파크",
    "특례보증", "육성자금", "이차보전", "지원사업 신청", "지원금 신청",
    "사업 참여기업 모집"
]

# ---------------------------------------------------------------------------
# 4. 중앙 통제식 RSS 피드 메타데이터 (Tier 및 Priority 완벽 적용)
# - Primary (우선순위 5) : 무조건 최우선 검토되는 A급 핵심 출처
# - Supplemental (우선순위 3~4) : 보조 출처 (LLM 후보군으로 주로 활용)
#   ✅ 3순위 반영: BCG Insights 제거 (https://www.bcg.com/rss 는 유효 피드가
#      아니라 매 실행 404. BCG 콘텐츠는 아래 'MBB/Big4 인사이트' Google News
#      쿼리가 커버함)
# ---------------------------------------------------------------------------
RSS_SOURCE_METADATA = {
    # 🌱 임팩트 (가장 중요 -> A급 매체 최대 포진)
    "Impact Alpha": {"url": "https://impactalpha.com/feed/", "category": "🌱 임팩트", "tier": "primary", "priority": 5},
    "NextBillion": {"url": "https://nextbillion.net/feed/", "category": "🌱 임팩트", "tier": "primary", "priority": 5},
    "SSIR": {"url": "https://ssir.org/site/rss_2.0/", "category": "🌱 임팩트", "tier": "primary", "priority": 5},
    "Pioneers Post": {"url": "https://www.pioneerspost.com/rss.xml", "category": "🌱 임팩트", "tier": "primary", "priority": 5},
    "Carbon Brief": {"url": "https://www.carbonbrief.org/feed/", "category": "🌱 임팩트", "tier": "primary", "priority": 5},
    "Responsible Investor": {"url": "https://www.responsible-investor.com/feed/", "category": "🌱 임팩트", "tier": "primary", "priority": 5},
    "ImpactOn (임팩트온)": {"url": "https://news.google.com/rss/search?q=(site:impacton.net)+when:3d&hl=ko&gl=KR&ceid=KR:ko", "category": "🌱 임팩트", "tier": "supplemental", "priority": 4},
    "Canary Media": {"url": "https://www.canarymedia.com/rss", "category": "🌱 임팩트", "tier": "supplemental", "priority": 4},
    "Climate Home News": {"url": "https://www.climatechangenews.com/feed/", "category": "🌱 임팩트", "tier": "supplemental", "priority": 4},

    # 🤖 AI (투자/규제/인프라 관점)
    "TechCrunch AI": {"url": "https://techcrunch.com/category/artificial-intelligence/feed/", "category": "🤖 AI", "tier": "primary", "priority": 5},
    "MIT Tech Review (AI)": {"url": "https://www.technologyreview.com/topic/artificial-intelligence/feed/", "category": "🤖 AI", "tier": "primary", "priority": 5},
    "SemiAnalysis": {"url": "https://www.semianalysis.com/feed", "category": "🤖 AI", "tier": "primary", "priority": 5},
    "The Batch": {"url": "https://news.google.com/rss/search?q=(site:deeplearning.ai/the-batch)+when:3d&hl=en-US&gl=US&ceid=US:en", "category": "🤖 AI", "tier": "primary", "priority": 5},  # original feed 404 → Google News fallback
    "The Verge AI": {"url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "category": "🤖 AI", "tier": "supplemental", "priority": 3},
    "Ars Technica": {"url": "https://feeds.arstechnica.com/arstechnica/index", "category": "🤖 AI", "tier": "supplemental", "priority": 3},

    # 📈 대체투자 (딜소싱 및 펀드 운용)
    "PE Hub": {"url": "https://www.pehub.com/feed/", "category": "📈 대체투자", "tier": "primary", "priority": 5},
    "Crunchbase News": {"url": "https://news.crunchbase.com/feed/", "category": "📈 대체투자", "tier": "primary", "priority": 5},
    "TechCrunch Venture": {"url": "https://techcrunch.com/category/venture/feed/", "category": "📈 대체투자", "tier": "primary", "priority": 5},
    "VCJ": {"url": "https://venturecapitaljournal.com/feed/", "category": "📈 대체투자", "tier": "primary", "priority": 5},
    "Sifted": {"url": "https://sifted.eu/feed", "category": "📈 대체투자", "tier": "supplemental", "priority": 4},
    "VentureSquare (벤처스퀘어)": {"url": "https://www.venturesquare.net/feed", "category": "📈 대체투자", "tier": "supplemental", "priority": 3},
    "Platum (플랫텀)": {"url": "https://platum.kr/feed", "category": "📈 대체투자", "tier": "supplemental", "priority": 3},
    "한경 Geeks (벤처/VC)": {"url": "https://rss.hankyung.com/feed/geeks.xml", "category": "🌐 거시·정책·지정학", "tier": "supplemental", "priority": 3},

    # 🌐 거시경제·정책·지정학
    "The Economist": {"url": "https://www.economist.com/finance-and-economics/rss.xml", "category": "🌐 거시·정책·지정학", "tier": "primary", "priority": 5},
    "Foreign Affairs": {"url": "https://www.foreignaffairs.com/rss.xml", "category": "🌐 거시·정책·지정학", "tier": "primary", "priority": 5},

    # 👔 MBB·Big4 인사이트
    "McKinsey Insights": {"url": "https://www.mckinsey.com/insights/rss", "category": "👔 MBB·Big4 인사이트", "tier": "primary", "priority": 5},
    "PwC strategy+business": {"url": "https://www.strategy-business.com/rss", "category": "👔 MBB·Big4 인사이트", "tier": "primary", "priority": 5},
    # 추가: 다른 MBB/Big4의 공식 인사이트 또는 블로그 피드(없는 경우 블로그로 대체)
    "Bain Insights": {"url": "https://www.bain.com/insights/feed/", "category": "👔 MBB·Big4 인사이트", "tier": "supplemental", "priority": 3},
    "BCG Insights (blog)": {"url": "https://news.google.com/rss/search?q=(site:bcg.com)+when:3d&hl=en-US&gl=US&ceid=US:en", "category": "👔 MBB·Big4 인사이트", "tier": "supplemental", "priority": 3},
    "BCG Korea 공식 블로그": {"url": "https://rss.blog.naver.com/bcg_kr.xml", "category": "👔 MBB·Big4 인사이트", "tier": "supplemental", "priority": 4},
    "Deloitte Insights (blog)": {"url": "https://news.google.com/rss/search?q=(site:deloitte.com+insights)+when:3d&hl=en-US&gl=US&ceid=US:en", "category": "👔 MBB·Big4 인사이트", "tier": "supplemental", "priority": 3},
    "EY Insights (blog)": {"url": "https://news.google.com/rss/search?q=(site:ey.com+insights)+when:3d&hl=en-US&gl=US&ceid=US:en", "category": "👔 MBB·Big4 인사이트", "tier": "supplemental", "priority": 3},
    "KPMG Insights (blog)": {"url": "https://news.google.com/rss/search?q=(site:kpmg.com+insights)+when:3d&hl=en-US&gl=US&ceid=US:en", "category": "👔 MBB·Big4 인사이트", "tier": "supplemental", "priority": 3},
}

# ---------------------------------------------------------------------------
# 5. 구글 뉴스 (🚀 when:3d 유지 & 정교한 AND 검색식 적용)
# ---------------------------------------------------------------------------
# 랭킹 가점용 관심 인물/기관/기업 (WATCHLIST_WEIGHT=2.5 로 가점). 비우면 신호 사라짐.
# 한국어 Google News 대비 한글명 병기.
ALL_WATCHLISTS = [
    "Trump", "트럼프", "Powell", "파월", "Federal Reserve", "연준", "FOMC",
    "이창용", "한국은행", "ECB", "유럽중앙은행",
    "Bank of Japan", "BOJ", "일본은행",
    "People's Bank of China", "PBOC", "중국인민은행",
    "OpenAI", "Anthropic", "NVIDIA", "엔비디아", "Google", "Microsoft",
    "삼성전자", "SK하이닉스",
]

OFFICIAL_INSIGHTS_CATEGORY = next(
    category for category in CATEGORIES if category.startswith("👔")
)

def _google_news_url(query: str, language: str = "en-US", country: str = "US") -> str:
    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl={language}&gl={country}&ceid={country}:{language[:2]}"
    )


# MBB·Big4는 회사명이 언급된 외부 기사가 아니라 공식 도메인의 발행물만 수집한다.
OFFICIAL_INSIGHTS_QUERIES = {
    "McKinsey Official Insights": "site:mckinsey.com/insights when:8d",
    "McKinsey Korea Insights": (
        "site:mckinsey.com/kr/our-insights "
        "(인사이트 OR 보고서 OR 연구 OR 전망 OR 트렌드 OR 산업) when:14d"
    ),
    "BCG Official Insights": "site:bcg.com/publications when:8d",
    "Bain Official Insights": "site:bain.com/insights when:8d",
    "Deloitte Official Insights": "site:deloitte.com/insights when:8d",
    "PwC Official Insights": 'site:pwc.com (insights OR "strategy+business") when:8d',
    "EY Official Insights": "site:ey.com (insights OR publications) when:8d",
    "KPMG Official Insights": "site:kpmg.com (insights OR research) when:8d",
    # 한국 법인이 직접 발행한 리포트·이슈 브리프·산업 전망을 별도로 수집한다.
    # 별도 국내 발행물 목록과 최근 검색 결과가 확인되는 공식 사이트만 포함한다.
    "국내 Deloitte Korea Insights": (
        "site:deloitte.com/kr/ko/our-thinking "
        "(보고서 OR 인사이트 OR 전망 OR 트렌드 OR research OR survey) when:8d"
    ),
    "국내 PwC Korea Insights": (
        "site:pwc.com/kr/ko/insights "
        "(보고서 OR 인사이트 OR 이슈브리프 OR 산업 OR 전망 OR 트렌드) when:8d"
    ),
    "국내 EY Korea Insights": (
        "(site:ey.com/ko_kr/insights OR site:ey.com/ko_kr/market-insights) "
        "(보고서 OR 인사이트 OR 전망 OR 트렌드 OR research OR survey) when:8d"
    ),
    "국내 KPMG Korea Insights": (
        "site:kpmg.com/kr/ko/insights "
        "(보고서 OR 인사이트 OR 전망 OR 트렌드 OR research OR survey) when:8d"
    ),
}
OFFICIAL_INSIGHTS_FEEDS = {
    feed_name: _google_news_url(
        query,
        language=(
            "ko"
            if feed_name.startswith("국내") or "Korea" in feed_name
            else "en-US"
        ),
        country=(
            "KR"
            if feed_name.startswith("국내") or "Korea" in feed_name
            else "US"
        ),
    )
    for feed_name, query in OFFICIAL_INSIGHTS_QUERIES.items()
}

# 다음 summarizer 단계에서 Google News 결과의 실제 언론사가 공식 회사인지 검증한다.
OFFICIAL_INSIGHTS_SOURCE_ALIASES = {
    "McKinsey Official Insights": ("McKinsey", "McKinsey & Company"),
    "McKinsey Korea Insights": (
        "McKinsey Korea", "McKinsey & Company", "McKinsey",
    ),
    "BCG Official Insights": ("BCG", "Boston Consulting Group"),
    "Bain Official Insights": ("Bain", "Bain & Company"),
    "Deloitte Official Insights": ("Deloitte", "Deloitte Insights"),
    "PwC Official Insights": ("PwC", "strategy+business"),
    "EY Official Insights": ("EY", "Ernst & Young"),
    "KPMG Official Insights": ("KPMG",),
    "BCG Korea 공식 블로그": ("BCG Korea", "BCG 코리아", "BCG"),
    "국내 Deloitte Korea Insights": (
        "Deloitte", "Deloitte Korea", "한국 딜로이트 그룹", "딜로이트",
    ),
    "국내 PwC Korea Insights": (
        "PwC", "Samil PwC", "삼일PwC", "삼일회계법인",
    ),
    "국내 EY Korea Insights": ("EY", "EY Korea", "EY한영"),
    "국내 KPMG Korea Insights": (
        "KPMG", "kpmg.com", "Samjong KPMG", "삼정KPMG",
    ),
}

# 다른 주제 피드로 들어와도 공식 도메인 발행물은 👔 인사이트로 고정한다.
OFFICIAL_INSIGHTS_DOMAINS = (
    "mckinsey.com",
    "bcg.com",
    "bain.com",
    "deloitte.com",
    "pwc.com",
    "strategy-business.com",
    "ey.com",
    "kpmg.com",
)

# 기사마다 개별 검색어를 추가하지 않고, 넓은 주제 하나를 설정해 수집과
# 카테고리 연결에 함께 사용한다. query_clauses는 모든 표현의 목록이 아니라
# 오탐을 막기 위해 반드시 함께 등장해야 하는 대표 개념 쌍이다.
DISCOVERY_TOPICS = {
    "국내 포용·상생금융": {
        "category": "🌱 임팩트",
        "description": (
            "금융기관과 자본시장이 투자자 보호, 금융 접근성, 취약계층의 "
            "자산형성, 모험자본 공급 등 사회적 역할을 확대하는 구조적 변화"
        ),
        "source_domains": (
            "mt.co.kr",
            "hankyung.com",
            "sedaily.com",
            "edaily.co.kr",
        ),
        "query_clauses": (
            ("증권업계", "사회 기여"),
            ("투자자 보호", "자산형성"),
            ("사회공헌", "금융투자협회"),
            ("포용금융", "취약계층"),
            ("상생금융", "자본시장"),
            ("금융교육", "청년"),
            ("모험자본", "사회적 역할"),
            ("공익펀드", "금융"),
        ),
        "window_days": 3,
    },
}


def _query_term(term: str) -> str:
    return f'"{term}"' if " " in term else term


def _discovery_topic_query(topic: dict) -> str:
    clause_query = "(" + " OR ".join(
        "(" + " ".join(_query_term(term) for term in clause) + ")"
        for clause in topic["query_clauses"]
    ) + ")"
    # Google News는 여러 site: 조건을 한 괄호에 모으면 검색 범위를 과도하게
    # 넓히는 경우가 있어, 출처별 완성 검색식을 만든 뒤 OR로 합친다.
    source_queries = " OR ".join(
        f"(site:{domain} {clause_query})"
        for domain in topic["source_domains"]
    )
    return (
        f"({source_queries}) "
        f"when:{topic.get('window_days', 3)}d"
    )


DISCOVERY_TOPIC_QUERIES = {
    topic_name: _discovery_topic_query(topic)
    for topic_name, topic in DISCOVERY_TOPICS.items()
}

SUPPLEMENTAL_NEWS_QUERIES = {
    **DISCOVERY_TOPIC_QUERIES,
    "글로벌 임팩트 주요 사건": (
        "(site:impactalpha.com OR site:nextbillion.net OR site:ssir.org OR "
        "site:pioneerspost.com OR site:canarymedia.com OR site:carbonbrief.org OR "
        "site:responsible-investor.com OR site:esgtoday.com) "
        '("impact investing" OR "climate tech" OR "social enterprise" OR '
        '"financial inclusion" OR "care economy" OR "circular economy" OR healthcare OR education) '
        "(funding OR raises OR acquisition OR policy OR regulation OR contract) when:3d"
    ),
    "국내 임팩트온": (
        "site:impacton.net (임팩트투자 OR 기후테크 OR 소셜벤처 OR 돌봄 OR 의료접근성 OR "
        "교육격차 OR 순환경제 OR ESG OR 사회적경제) when:3d"
    ),
    "국내 기후에너지경제": (
        "site:efn.co.kr (기후 OR 에너지 OR 탄소 OR 전력망 OR 재생에너지 OR "
        "환경정책 OR 녹색금융) when:3d"
    ),
    "국내 소셜임팩트뉴스": (
        "site:socialimpactnews.net (임팩트 OR 사회적경제 OR 소셜벤처 OR 돌봄 OR "
        "헬스케어 OR 교육 OR 장애 OR 비영리) when:3d"
    ),
    "국내 한경ESG": (
        "site:hankyung.com/esg (ESG OR 기후 OR 탄소 OR 임팩트 OR 지속가능 OR "
        "공급망 OR 책임투자 OR 녹색금융) when:3d"
    ),
    "국내 딜사이트 VC/PE": (
        "site:dealsite.co.kr/articles "
        "(투자유치 OR 펀드결성 OR 인수 OR 합병 OR 매각 OR IPO OR 상장 OR 출자) when:3d"
    ),
    # 마켓인사이트 공개 목록은 GitHub Actions 요청을 403으로 차단한다.
    # 기사 페이지는 Google News에 색인되므로 site: 검색으로만 보완한다.
    "국내 한경 마켓인사이트": (
        "site:marketinsight.hankyung.com/article "
        "(투자유치 OR 자금조달 OR 펀드 OR 출자 OR 인수 OR 합병 OR 매각 OR "
        "IPO OR 상장 OR 기업가치 OR 벤처캐피탈 OR 사모펀드) when:3d"
    ),
    "국내 플래텀 VC/스타트업": (
        "site:platum.kr (투자 OR 투자유치 OR 펀드 OR 인수 OR 합병 OR 매각 OR "
        "IPO OR 상장 OR 벤처 OR 스타트업) when:3d"
    ),
    "국내 더벨 VC/PE": (
        "site:thebell.co.kr (투자 OR 투자유치 OR 펀드 OR 출자 OR 인수 OR 합병 OR "
        "매각 OR IPO OR 상장 OR 벤처캐피탈 OR 사모펀드) when:3d"
    ),
    "국내 스타트업레시피 VC": (
        "site:startuprecipe.co.kr (투자 OR 투자유치 OR 펀드 OR 인수 OR 합병 OR "
        "매각 OR IPO OR 상장 OR 벤처 OR 스타트업) when:3d"
    ),
    "국내 벤처스퀘어 VC/스타트업": (
        "site:venturesquare.net (투자 OR 투자유치 OR 펀드 OR 인수 OR 합병 OR "
        "매각 OR IPO OR 상장 OR 벤처 OR 스타트업) when:3d"
    ),
    # 국내 거시 보완은 매체를 무작정 늘리지 않고 연합뉴스·한국경제로 한정한다.
    # 광범위한 '경제' 검색 대신 VC 판단에 직접 영향을 주는 거시·정책 신호만 수집한다.
    "국내 거시/정책 (연합·한경)": (
        "(site:yna.co.kr OR site:hankyung.com) "
        "(\"한국은행\" OR \"한은 금리\" OR \"한국 성장률\" OR \"한국 경제성장률\" OR "
        "\"가계부채\" OR \"국내 소비자물가\" OR \"정부 경제정책\" OR \"내년도 예산\" OR "
        "\"한국 수출\" OR \"국내 고용\") when:3d"
    ),
    "Bloomberg Green": "site:bloomberg.com/green (climate OR energy OR carbon) when:3d",
    "ESG Today": "site:esgtoday.com (investment OR regulation OR policy OR financing) when:3d",
    "VentureBeat AI": "site:venturebeat.com/category/ai when:3d",
    "Business Insider VC": (
        'site:businessinsider.com ("venture capital" OR "private equity" OR '
        '"AI funding" OR "climate tech") when:3d'
    ),
}
SUPPLEMENTAL_NEWS_FEEDS = {
    feed_name: _google_news_url(
        query,
        language="ko" if feed_name.startswith("국내") else "en-US",
        country="KR" if feed_name.startswith("국내") else "US",
    )
    for feed_name, query in SUPPLEMENTAL_NEWS_QUERIES.items()
}

# Google News에 색인되지 않는 신뢰 매체는 공개 기사 목록을 직접 읽는다.
# 다음 수집 단계는 아래 설정만 보고 목록·제목·요약·날짜를 추출하도록 구성한다.
DIRECT_WEB_SOURCE_METADATA = {
    "McKinsey Korea Insights": {
        "enabled": False,
        "url": "https://www.mckinsey.com/kr/our-insights/mckinsey-insights",
        "category": "👔 MBB·Big4 인사이트",
        "tier": "primary",
        "priority": 5,
        "region": "korea",
        "parser": "semantic_links",
        "allowed_domains": ("mckinsey.com",),
        "article_path_markers": ("/our-insights/", "/our-research/"),
        "minimum_title_length": 12,
        "lookback_days": 14,
        "require_date": True,
    },
    # GitHub Actions에서는 공개 목록이 403을 반환하므로 실제 수집은 끈다.
    # 파서 설정은 과거 데이터 재현과 회귀 테스트를 위해 보존하고, 위의
    # Google News site: 검색이 신규 기사를 대신 수집한다.
    "국내 한경 마켓인사이트": {
        "enabled": False,
        "url": "https://marketinsight.hankyung.com/freenews",
        "category": "📈 대체투자",
        "tier": "supplemental",
        "priority": 4,
        "region": "korea",
        "article_url_prefix": "https://marketinsight.hankyung.com/article/",
        "title_container_class": "news-tit",
        "summary_class": "lead",
        "date_from_url_pattern": r"/article/(?P<date>\d{8})",
    },
}

# 해외 거시 보완 검색은 핵심 시장의 정책·지표를 기본으로 하고, 그 밖의
# 국가는 세계 금융시장·에너지·공급망으로 전파되는 사건만 수집한다.
_MACRO_CORE_MARKETS_QUERY = (
    '("Federal Reserve" OR "Fed" OR FOMC OR "United States" OR "U.S." OR '
    '"US economy" OR "US inflation" OR "US jobs" OR "US GDP" OR '
    'ECB OR eurozone OR "European Union" OR Germany OR France OR Italy OR Spain OR '
    '"UK economy" OR "UK inflation" OR "Bank of England" OR '
    '"Bank of Japan" OR BOJ OR Japan OR '
    '"People\'s Bank of China" OR PBOC OR China OR '
    '"Bank of Korea" OR "South Korea economy")'
)
_MACRO_DECISION_SIGNALS_QUERY = (
    '("interest rate" OR inflation OR GDP OR growth OR employment OR unemployment OR '
    'recession OR tariff OR "trade policy" OR sanctions OR regulation OR '
    '"fiscal policy" OR budget)'
)
_MACRO_GLOBAL_SHOCK_QUERY = (
    '("global markets" OR "world economy" OR "oil prices" OR OPEC OR '
    '"Strait of Hormuz" OR "Red Sea" OR "Suez Canal" OR '
    '"global supply chain" OR "trade route" OR "trade war" OR '
    '"sovereign default" OR "currency crisis" OR "financial contagion" OR '
    '((Russia OR Ukraine OR Israel OR Iran OR Taiwan OR "Middle East") '
    '(war OR attack OR invasion OR blockade OR sanctions)))'
)


def _macro_source_query(domain: str) -> str:
    return (
        f"site:{domain} "
        f"(({_MACRO_CORE_MARKETS_QUERY} {_MACRO_DECISION_SIGNALS_QUERY}) "
        f"OR {_MACRO_GLOBAL_SHOCK_QUERY}) when:3d"
    )

GOOGLE_NEWS_FEEDS = {
    **OFFICIAL_INSIGHTS_FEEDS,
    **SUPPLEMENTAL_NEWS_FEEDS,
    "글로벌 VC/PE": _google_news_url(
        "(site:pehub.com OR site:news.crunchbase.com OR site:techcrunch.com OR "
        "site:sifted.eu OR site:businessinsider.com OR site:bloomberg.com) "
        '("venture capital" OR "private equity" OR "funding round" OR '
        '"dry powder" OR "startup raising" OR acquisition OR IPO) when:3d'
    ),
    "Reuters 거시/정책": _google_news_url(
        _macro_source_query("reuters.com")
    ),
    "Bloomberg 거시/정책": _google_news_url(
        _macro_source_query("bloomberg.com")
    ),
}

# ---------------------------------------------------------------------------
# 5-0. Google News 피드 → 카테고리 강제 매핑
#   Google News 기사는 RSS_SOURCE_METADATA 에 category 가 없어 '키워드 분류'로 떨어진다.
#   공식 인사이트와 명확한 딜·거시 검색만 피드명으로 고정한다.
#   임팩트 보완 검색은 잘못된 강제 분류를 막기 위해 내용 판정을 거친다.
# ---------------------------------------------------------------------------
FEED_CATEGORY_OVERRIDE = {
    **{
        feed_name: OFFICIAL_INSIGHTS_CATEGORY
        for feed_name in OFFICIAL_INSIGHTS_FEEDS
    },
    **{
        topic_name: topic["category"]
        for topic_name, topic in DISCOVERY_TOPICS.items()
    },
    **{
        feed_name: "🌱 임팩트"
        for feed_name in (
            "국내 임팩트온",
            "국내 기후에너지경제",
            "국내 소셜임팩트뉴스",
            "국내 한경ESG",
        )
    },
    **{
        feed_name: "📈 대체투자"
        for feed_name in (
            "국내 딜사이트 VC/PE",
            "국내 한경 마켓인사이트",
            "국내 플래텀 VC/스타트업",
            "국내 더벨 VC/PE",
            "국내 스타트업레시피 VC",
            "국내 벤처스퀘어 VC/스타트업",
        )
    },
    "글로벌 VC/PE": "📈 대체투자",
    "McKinsey Korea Insights": OFFICIAL_INSIGHTS_CATEGORY,
    "국내 거시/정책 (연합·한경)": "🌐 거시·정책·지정학",
    "Reuters 거시/정책": "🌐 거시·정책·지정학",
    "Bloomberg 거시/정책": "🌐 거시·정책·지정학",
}

# ---------------------------------------------------------------------------
# 5-1. ✅ 1순위 반영: 수집용 피드 통합 (Google News 병합!)
#      rss_feeds.fetch() 는 ALL_FEEDS 만 순회하므로, 여기서 병합해야
#      FOMC·트럼프·국내외 VC·MBB/Big4 Google News 쿼리가 전부 수집된다.
#      (반드시 GOOGLE_NEWS_FEEDS 정의 '이후'에 위치해야 함)
# ---------------------------------------------------------------------------
# Only feeds verified as RSS/XML endpoints are eligible for primary ingestion.
# Google News remains in GOOGLE_NEWS_FEEDS and is merged only after this list.
VERIFIED_RSS_SOURCE_NAMES = frozenset({
    "Impact Alpha",
    "NextBillion",
    "SSIR",
    "Pioneers Post",
    "Carbon Brief",
    "Responsible Investor",
    "TechCrunch AI",
    "MIT Tech Review (AI)",
    "SemiAnalysis",
    "PE Hub",
    "Crunchbase News",
    "TechCrunch Venture",
    "Sifted",
    "The Economist",
    "Foreign Affairs",
    "McKinsey Insights",
    "BCG Korea 공식 블로그",
})

# ImpactOn is the single domestic supplement retained for the impact category.
# Use its direct RSS endpoint instead of routing this RSS source through Google News.
RSS_SOURCE_METADATA = {
    source_name: (
        {**metadata, "url": "https://www.impacton.net/rss/allArticle.xml"}
        if source_name.startswith("ImpactOn")
        else metadata
    )
    for source_name, metadata in RSS_SOURCE_METADATA.items()
    if source_name in VERIFIED_RSS_SOURCE_NAMES or source_name.startswith("ImpactOn")
}

# A category/tier registry is derived from the source metadata. Adding a source
# requires only one config entry; it will be collected in category priority order.
CATEGORY_RSS_SOURCES = {
    category: {
        tier: {
            source_name: metadata
            for source_name, metadata in RSS_SOURCE_METADATA.items()
            if metadata["category"] == category and metadata["tier"] == tier
        }
        for tier in ("primary", "supplemental")
    }
    for category in CATEGORIES
}

RSS_FEEDS = {
    source_name: metadata["url"]
    for category in CATEGORIES
    for tier in ("primary", "supplemental")
    for source_name, metadata in CATEGORY_RSS_SOURCES[category][tier].items()
}

ALL_FEEDS = {
    **RSS_FEEDS,
    **GOOGLE_NEWS_FEEDS
}

RSS_SOURCES = ALL_FEEDS

# ---------------------------------------------------------------------------
# 5-2. 지역 판별 (⚠️ ALL_FEEDS 정의 이후여야 하므로 이 위치 유지)
# Source names contain localized display text, so derive the Korean-source set
# from stable feed domains rather than duplicating those display names here.
# ---------------------------------------------------------------------------
_KOREA_SOURCE_DOMAINS = (
    "impacton.net",
    "platum.kr",
    "venturesquare.net",
    "hankyung.com",
    "etnews.com",
    "blog.naver.com",
)
KOREA_SOURCE_NAMES = frozenset(
    source_name
    for source_name, feed_url in ALL_FEEDS.items()
    if any(domain in feed_url for domain in _KOREA_SOURCE_DOMAINS)
    or "ceid=KR" in feed_url          # 한국어 Google News 쿼리도 국내 취급
)


def source_region(source_name: str) -> str:
    """Return the configured region for a known feed source."""
    return "korea" if source_name in KOREA_SOURCE_NAMES else "global"

# ---------------------------------------------------------------------------
# (참고) 과거의 SOURCE_CATEGORY_OVERRIDE 는 summarizer 가 참조하지 않는 죽은 변수라 삭제함.
#   현재 카테고리 배정은 RSS_SOURCE_METADATA['category'] 가 담당.
#   ※ 단, Google News 5개 피드는 메타데이터에 category 가 없어 '키워드 분류'로 떨어짐.
#     'MBB/Big4 인사이트' 구글뉴스가 👔 인사이트로 안 꽂히면, summarizer 에
#     feed명→category 매핑을 추가해야 함(Step 4 대상).
# ---------------------------------------------------------------------------
