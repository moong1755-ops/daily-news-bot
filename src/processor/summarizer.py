import re
from ..config import (
    CATEGORIES,
    SOFT_PENALTY_KEYWORDS,
    WATCHLIST_WEIGHT,
    ALL_WATCHLISTS,
    RSS_SOURCE_METADATA,      # 🚀 메타데이터 임포트
    FEED_CATEGORY_OVERRIDE,   # ✅ Google News 피드 → 카테고리 강제 매핑
    DEAL_PRIORITY_SIGNALS,
    DEAL_EARLY_STAGE_SIGNALS,
    DEAL_EXCLUSION_KEYWORDS,
    IMPACT_EARLY_STAGE_SIGNALS,
    EDITORIAL_PRIORITY_SIGNALS,
    EDITORIAL_PRIORITY_WEIGHT,
    EDITORIAL_EXCLUSION_KEYWORDS,
    IMPACT_THEME_KEYWORDS,
    OFFICIAL_INSIGHTS_DOMAINS,
    OFFICIAL_INSIGHTS_SOURCE_ALIASES,
)


# 단독으로는 임팩트 투자 기사라는 근거가 부족한 넓은 산업 용어.
# 전문 임팩트 출처이거나 접근성·형평성·성과 같은 목적 신호가 함께 있어야 한다.
_BROAD_IMPACT_KEYWORDS = {
    "healthcare", "digital health", "healthtech", "mental health",
    "education", "edtech", "esg", "sustainability",
    "헬스케어", "디지털헬스", "헬스테크", "정신건강",
    "교육", "에듀테크", "지속가능",
}

_IMPACT_PURPOSE_SIGNALS = [
    "impact investing", "social impact", "measurable impact", "underserved",
    "low-income", "vulnerable communities", "public benefit", "patient outcomes",
    "learning outcomes", "emissions reduction", "reduces emissions", "health access",
    "임팩트투자", "사회적 가치",
    "취약계층", "저소득", "공공성",
    "의료접근성", "교육격차", "학습성과", "탄소 감축",
]

# 넓은 ESG·지속가능성 표현만으로 임팩트로 보내지는 않되, 실제 임팩트
# 투자·사업 모델을 뜻하는 구체적인 표현은 출처와 무관하게 임팩트를 우선한다.
_SPECIFIC_IMPACT_BUSINESS_SIGNALS = [
    "impact investor", "impact fund", "impact capital", "climate fund",
    "climate finance", "climate tech", "clean energy", "renewable energy",
    "energy transition", "transition finance", "decarbonization", "low-carbon",
    "carbon accounting", "carbon credits", "sustainability reporting",
    "sustainable development bond", "green bond", "social bond",
    "sustainability bond", "transition bond",
    "circular economy", "financial inclusion", "affordable housing",
    "임팩트 투자", "임팩트투자", "임팩트 펀드", "기후 펀드", "기후금융",
    "기후테크", "청정에너지", "재생에너지", "에너지 전환", "전환금융",
    "탈탄소", "탄소회계", "탄소배출권", "지속가능성 보고", "순환경제",
    "지속가능개발채권", "녹색채권", "사회적채권", "지속가능채권", "전환채권",
    "금융포용", "주거복지",
]

# 거시는 경제 전체의 방향을 바꾸는 통화·물가·성장·고용·재정·무역·지정학
# 사건만 인정한다. '규제', '법', '제재' 같은 단어 하나로 기업·생활 행정
# 기사를 거시로 보내지 않도록 문맥이 포함된 정규식으로 판정한다.
_STRICT_MACRO_PATTERNS = [
    # 통화정책·핵심 경제지표
    r"\b(?:federal reserve|central banks?|bank of korea|european central bank|"
    r"bank of japan|people'?s bank of china)\b",
    r"\b(?:monetary policy|interest rates?|policy rates?|inflation|consumer prices?|cpi|"
    r"gross domestic product|gdp|recession|economic growth|exchange rates?|"
    r"treasury yields?|government bond yields?|unemployment|employment|payrolls?)\b",
    r"\b(?:fiscal policy|budget deficit|national debt|trade balance)\b",
    r"연방준비제도|연준|중앙은행|한국은행|유럽중앙은행|일본은행|중국인민은행|한은",
    r"통화정책|기준금리|정책금리|인플레이션|소비자물가|국내총생산|경제성장률|"
    r"경기침체|환율|국채금리|실업률|고용률|고용동향|재정정책|국가채무|국가부채|정부부채|무역수지",
    # 정부·의회가 경제 전반을 다루는 경우에만 정책 기사로 인정
    r"\b(?:government|parliament|congress|ministry|regulator)\b.{0,80}"
    r"\b(?:economic policy|fiscal|tax reform|national budget|labor market|"
    r"financial system|capital markets?|banking sector|trade policy)\b",
    r"\b(?:economic policy|fiscal|tax reform|national budget|labor market|"
    r"financial system|capital markets?|banking sector|trade policy)\b.{0,80}"
    r"\b(?:government|parliament|congress|ministry|regulator)\b",
    r"(?:정부|국회|기획재정부|금융위원회|금융당국).{0,40}"
    r"(?:경제정책|재정|세제|국가예산|정부예산|노동시장|금융시장|자본시장|은행권|고용)",
    r"(?:경제정책|재정|세제|국가예산|정부예산|노동시장|금융시장|자본시장|은행권|고용)"
    r".{0,40}(?:정부|국회|기획재정부|금융위원회|금융당국)",
    # 무역·지정학. '관세청'의 관세, 국내 행정처분의 제재는 제외한다.
    r"\btariffs?\b|\btrade war\b|\bexport controls?\b|\bgeopolitics?\b|"
    r"\bceasefire\b|\bmilitary conflict\b|\binvasion\b",
    r"\b(?:economic|trade|international|western|un|u\.s\.|eu|russia|china|iran|"
    r"north korea) sanctions?\b",
    r"\bsanctions?\b.{0,50}\b(?:russia|china|iran|north korea)\b",
    r"관세(?!청)|무역전쟁|수출통제|지정학|전쟁|휴전|군사분쟁|침공|대통령\s*선거|총선",
    r"(?:대북|대러|대중|미국|유럽연합|유럽|유엔|국제).{0,30}제재|"
    r"제재.{0,30}(?:북한|러시아|중국|이란)",
]

# 대체투자·거시 브리핑의 '국내/해외'는 언론사 소재지가 아니라
# 사건이 실제로 발생했거나 직접 영향을 받는 시장을 기준으로 한다.
_REGION_SPLIT_CATEGORY_PREFIXES = ("📈", "🌐")
_KOREA_EVENT_SIGNALS = [
    "south korea", "korean", "bank of korea", "seoul", "kospi", "kosdaq",
    "한국", "국내", "우리나라", "한은", "한국은행", "원화", "코스피", "코스닥",
    "기획재정부", "산업통상자원부", "중소벤처기업부", "금융위원회",
    "금융감독원", "공정거래위원회", "국회", "서울", "부산",
]
_FOREIGN_EVENT_SIGNALS = [
    "united states", "u.s.", "federal reserve", "european union", "eurozone",
    "european central bank", "bank of england", "people's bank of china",
    "united kingdom", "canada", "china", "japan", "india", "russia",
    "ukraine", "middle east", "g7", "g20",
    "미국", "미 연준", "연준", "유럽연합", "유로존", "유럽중앙은행", "ecb",
    "영국", "캐나다", "중국", "일본", "인도", "러시아", "우크라이나", "중동",
    "美", "中", "日", "英", "歐", "俄", "美국채", "美연준", "美재무부", "美증시",
]

# AI 데이터센터의 광섬유·네트워크·전력·냉각 인프라는 사용자의 편집 원칙에
# 따라 투자 기사여도 AI 카테고리에 둘 수 있다.
_AI_INFRASTRUCTURE_SIGNALS = [
    "fiber", "fibre", "interconnect", "optical network", "optical networking",
    "photonics",
    "cooling", "power infrastructure", "data center infrastructure",
    "orbital data center", "orbital data centers",
    "space data center", "space data centers",
    "광섬유", "광네트워크", "인터커넥트", "광통신", "포토닉스", "냉각",
    "전력 인프라", "데이터센터 인프라",
]

# AI 기업이더라도 IPO·상장은 기술 뉴스가 아니라 자본시장 사건이므로
# 대체투자에 배정한다. 제품 기사 본문에 '상장사'가 우연히 등장하는 경우를
# 피하기 위해 제목에서 실제 상장 절차를 뜻하는 표현만 인정한다.
_PUBLIC_MARKET_EVENT_PATTERNS = [
    r"\bipo\b", r"\binitial public offering\b", r"\bfiles? for (?:an )?ipo\b",
    r"\bplans? to (?:go public|list)\b", r"\bpublic listing\b",
    r"기업공개", r"상장(?:예비심사|심사|추진|준비|시동|신청|승인|계획|절차|돌입|예정|완료)",
    r"(?:코스닥|코스피|나스닥|뉴욕증시).{0,12}상장",
]
_PRE_IPO_EVENT_PATTERNS = [
    r"\bpre[- ]?ipo\b", r"프리\s*ipo", r"상장\s*전\s*투자\s*유치",
]

# 제목의 중심 사건이 소송·벌금·합의인 경우, 본문이나 법률 용어 속의
# 'merger/investment' 한 단어를 실제 거래로 오인하지 않는다. 편집 게이트는
# 시장 전체에 중요한 규제 선례라면 다시 살릴 수 있지만, LLM 장애 시에는
# 일반 법률 기사가 대체투자 칸을 차지하지 않는 쪽이 안전하다.
_NON_DEAL_LEGAL_EVENT_PATTERNS = [
    r"\b(?:settles?|settlement|lawsuit|litigation|fines?|penalt(?:y|ies)|damages)\b",
    r"\b(?:agrees?|ordered) to pay\b",
    r"소송|합의금|벌금|과징금|손해배상|배상금",
]

# 제목만으로 확실하게 판별할 수 있는 편집 제외 대상. 설명문에 우연히 같은
# 표현이 등장해 정상 기사를 지우지 않도록 제목에만 적용한다.
_TITLE_NOISE_PATTERNS = [
    r"\broadshow\b", r"\bwebinar\b", r"\bconference invite\b",
    r"\bregister now\b", r"\bevent registration\b", r"\bseminar\b",
    r"\bcoffee with\b", r"\binterview with\b", r"\bq\s*&\s*a\b",
    r"\bconversation with\b", r"\bpodcast\b",
    r"\bmou\b", r"\bmemorandum of understanding\b",
    r"업무협약", r"협약 체결", r"로드쇼", r"웨비나", r"세미나",
    r"인터뷰", r"대담", r"팟캐스트", r"설명회", r"캠페인",
    # 설명 없는 단독 그래픽과 정례 시장조작 공지는 주요 변화 기사로 보지 않는다.
    r"^\s*[\[(（【]\s*그래픽\s*[\])）】]",
    r"(?:한은|한국은행).{0,20}통화안정증권.{0,20}발행",
    # 회계기준·세무 단순 실무 공지는 시장 인사이트가 아니라서 제외한다.
    r"\b(?:fasb|ifrs|gaap|accounting standards?|effective dates?|tax alert|weekly accounting news)\b",
    r"\baccounting for\b",
    r"(?:회계기준|회계\s*처리|세무\s*알림|세법\s*개정\s*안내)\b",
]

# MBB·Big4 공식 블로그는 인사이트로 인정하지만, 일반 매체의 블로그·게스트
# 글은 사실 보도보다 의견·해설 성격이 강해 최종 브리핑에서 제외한다.
_NON_OFFICIAL_BLOG_PATTERNS = [
    r"[\[(]\s*(?:blog|guest blog)\s*[\])]",
    r"\bguest (?:post|essay)\b",
]

# 개별 상장사의 자사주·배당 기사는 VC/PE 딜이 아니다. 단, 관련 법·제도
# 변경은 투자시장에 중요하므로 제외 대상에서 다시 구제한다.
_LISTED_COMPANY_ACTION_PATTERNS = [
    r"\bshare buybacks?\b", r"\bstock repurchases?\b",
    r"\bdividend (?:hike|increase|boost)\b",
    r"자사주[^\n…|]{0,30}(?:매입|취득|소각)", r"주주환원(?:책)?",
    r"배당\s*(?:확대|상향|증액)",
]
_CAPITAL_MARKET_POLICY_PATTERNS = [
    r"\b(?:law|bill|rule|regulation|mandate|proposal|reform)\b",
    r"법안|의무화|규제|상법|정부|국회|금융위원회|금융당국|제도\s*개편",
]

_GENERIC_INSIGHT_PAGE_PATTERNS = [
    r"^insights\s*[-|–—]\s*[^:]+$", r"^our insights$",
    r"^latest insights$", r"^insights and publications$",
    r"^research and insights$", r"^인사이트$", r"^최신 인사이트$",
]

_OFFICIAL_PERSON_VIEW_TITLE_PATTERN = (
    r"^(?:McKinsey|BCG|Bain|Deloitte|PwC|EY|KPMG)(?:['’]s)\s+"
    r"(?!Global\b|Annual\b|New\b|Latest\b|Report\b|Research\b|Survey\b|"
    r"Analysis\b|State\b)"
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+"
    r"(?:\s+(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+|de|van|von|da|di)){1,3}\s+on\b"
)

# 공식 도메인 검색에는 리포트뿐 아니라 임직원 약력, 채용, 서비스 소개,
# 사무소 안내 페이지도 함께 노출된다. 일반 기사에는 적용하지 않고 검증된
# MBB·Big4 공식 피드의 제목에만 적용해 정상적인 시장 리포트를 보호한다.
_OFFICIAL_INSIGHT_NOISE_PATTERNS = [
    # 임직원·전문가 프로필
    r"\b(?:leader|partner|principal|director|managing director|chief economist)\b"
    r".*\|\s*(?:ey|pwc|deloitte|kpmg|mckinsey|bain|bcg)\b",
    r"\b(?:ey|pwc|deloitte|kpmg|mckinsey|bain|bcg)\b.*"
    r"\b(?:leader|partner|principal|director|managing director)\b",
    r"(?:대표|파트너|전무|상무|본부장|부문장|리더|회계사|변호사)\s*"
    r"(?:\||[-–—])\s*(?:ey|pwc|deloitte|kpmg|맥킨지|베인|bcg)\b",
    r"^(?:our people|people and offices|leadership|meet our people)$",
    r"^(?:임직원|전문가|리더십|경영진|구성원)\s*(?:소개)?$",
    # 채용 시스템에 등록된 직무 제목. "manager" 같은 단어 하나만으로는
    # 정상 경영 리포트를 지울 수 있어 직급 조합이나 근무지 괄호가 있을 때만 막는다.
    r"\b(?:senior|junior|experienced|entry[- ]level)\s+"
    r"(?:associate|consultant|manager|analyst|specialist)\b"
    r"(?:\s*\([^)]*\))?\s*$",
    r"\b(?:associate|consultant|manager|analyst|specialist|intern)\b"
    r"\s*\([^)]*\)\s*$",
    r"\b(?:job opening|job opportunity|career opportunity|vacancy|recruitment)\b",
    r"(?:신입|경력|인턴|매니저|컨설턴트|어소시에이트|애널리스트)\s*"
    r"(?:채용|모집)(?:\s*\([^)]*\))?\s*$",
    # 서비스·조직·사무소 안내
    r"\b(?:services?|solutions?|capabilities|careers|locations?|offices?)\b"
    r"\s*(?:\||[-–—])\s*(?:ey|pwc|deloitte|kpmg|mckinsey|bain|bcg)\b",
    r"\binsights?\s*&\s*services\b",
    r"(?:서비스|솔루션|채용|사무소|오피스|조직)\s*(?:소개|안내)?\s*"
    r"(?:\||[-–—])\s*(?:ey|pwc|deloitte|kpmg|맥킨지|베인|bcg)\b",
]

_COMPOUND_ROUNDUP_PATTERNS = [
    r"【\s*esg deal\s*】", r"^esg deal\s*[:：]", r"^deal roundup\b",
    r"^the week in\b", r"\bweekly roundup\b", r"\bweekly recap\b",
    r"\bweek in review\b", r"^news roundup\b", r"^the brief\s*[:：]",
    r"\bthey said it\b",
    r"^\s*[\[(（【]\s*뉴스\s*모음\s*[\])）】]",
    r"^\s*[\[(（]?\s*week ahead\b", r"^\s*[\[(（]?\s*weekly calendar\b",
    r"^주간\s*(?:모음|정리|리뷰)\b", r"^이번\s*주\s*(?:모음|정리|리뷰)\b",
    r"^\s*[\[(（]?\s*(?:다음|이번)\s*주\s*(?:경제|증시|산업|정책|일정)\s*[\])）]?",
    r"^\s*[\[(（]?\s*주간\s*(?:경제|증시|산업|정책)?\s*일정\s*[\])）]?",
]

# 같은 M&A·투자 기사라도 확정, 협상, 검토, 전망, 무산은 투자자에게
# 서로 다른 정보다. 루머를 버리지 않되 확정 거래로 오인하지 않게 구분한다.
_EVENT_STATUS_PATTERNS = [
    (
        "adverse_confirmed",
        [
            r"\b(?:withdrawn|scrapped|called off|collapsed|abandoned|cancelled|canceled|failed)\b",
            r"철회|무산|결렬|좌초|불발|중단|포기|인증 취소|무효화",
            r"상장\s*(?:연기|철회|불투명)|ipo\s*(?:연기|철회|불투명)",
            r"회수\s*(?:차질|지연)|가치(?:가|는)?\s*(?:급락|하락|증발)|못\s*미쳐",
        ],
    ),
    (
        "confirmed",
        [
            r"\b(?:acquired|acquires|bought|buys|raised|raises|closed|closes|signed|wins)\b",
            r"\bto acquire\b|\bhas raised\b|\bcompleted\b|\bapproved\b",
            r"투자\s*유치(?!\s*(?:를\s*)?(?:검토|추진|계획|예정|목표|모색|타진|준비|가능성))(?:\s*(?:완료|성공|확정))?",
            r"자금\s*조달(?!\s*(?:을\s*)?(?:검토|추진|계획|예정|목표|모색|타진|준비|가능성))(?:\s*(?:완료|성공))?",
            r"인수(?:를)?\s*(?:완료|확정|했다|한다)|매각(?:을)?\s*(?:완료|확정|했다|한다)",
            r"계약\s*체결(?!\s*(?:을\s*)?(?:검토|추진|계획|예정|목표|모색|타진|준비|가능성))",
            r"최종\s*클로징|(?:상업\s*운전|상용\s*가동)(?!\s*(?:을\s*)?(?:검토|추진|계획|예정|목표|준비))|가동(?:에)?\s*성공",
            r"실증(?:에)?\s*성공(?!\s*(?:을\s*)?(?:목표|계획|예정))|승인(?:을)?\s*(?:받았다|획득|완료|확정)",
        ],
    ),
    (
        "in_progress",
        [
            r"\b(?:talks|negotiating|negotiations|bidding|due diligence)\b",
            r"\bin discussions\b|\bseeking bids\b",
            r"협상|논의\s*중|입찰\s*중|실사\s*중|우선협상대상|가격\s*조율|조건\s*협의",
        ],
    ),
    (
        "considering",
        [
            r"\b(?:considering|explores|exploring|weighs|mulls|plans to|seeks to)\b",
            r"검토|추진|계획|예정|모색|타진|준비\s*중|가능성",
        ],
    ),
    (
        "outlook",
        [
            r"\b(?:outlook|forecast|prediction|survey|expected to)\b",
            r"전망|예측|관측|시장\s*동향|산업\s*동향|트렌드\s*분석",
        ],
    ),
]

_REPORTING_BASIS_PATTERNS = [
    (
        "official_announcement",
        [
            r"\b(?:announced|filing|according to the company|according to the regulator)\b",
            r"공시|공식\s*발표|회사(?:는|가|측이)\s*밝혔다|정부(?:는|가)\s*발표|당국(?:은|이)",
        ],
    ),
    (
        "sourced_report",
        [
            r"\b(?:sources say|reportedly|people familiar|person familiar)\b",
            r"복수의\s*소식통|소식통|관계자에\s*따르면|정통한\s*관계자|취재에\s*따르면",
        ],
    ),
    (
        "unconfirmed_rumor",
        [
            r"\b(?:rumou?r|unconfirmed)\b",
            r"인수설|매각설|합병설|투자설|상장설|루머|소문|미확인",
        ],
    ),
    (
        "analysis",
        [r"\b(?:analysis|outlook|forecast|report)\b", r"전망|예측|분석|보고서|설문"],
    ),
]

_EVENT_STATUS_LABELS = {
    "confirmed": "확정",
    "in_progress": "협상·진행중",
    "considering": "검토·추진",
    "reported_unconfirmed": "미확인 보도",
    "outlook": "전망·분석",
    "adverse_confirmed": "철회·무산·위험",
    "unknown": "상태 미분류",
}

_REPORTING_BASIS_LABELS = {
    "official_announcement": "공식발표·공시",
    "sourced_report": "취재원 보도",
    "unconfirmed_rumor": "미확인 루머",
    "analysis": "분석·전망",
    "unspecified": "근거 미분류",
}

_EVENT_STATUS_WEIGHTS = {
    "confirmed": 1.5,
    "adverse_confirmed": 1.5,
    "in_progress": 1.0,
    "considering": 0.5,
    "reported_unconfirmed": 0.5,
    "outlook": 0.25,
    "unknown": 0.0,
}

def keyword_hit(keyword: str, text: str) -> bool:
    kw_lower = keyword.lower()
    if re.search(r'[a-z]', kw_lower):
        pattern = r'\b' + re.escape(kw_lower) + r'\b'
        return bool(re.search(pattern, text))
    else:
        return kw_lower in text


def _has_any(keywords: list, text: str) -> bool:
    return any(keyword_hit(keyword, text) for keyword in keywords)


def _content_region(article: dict, category: str, text: str) -> tuple[str, str]:
    """Classify split sections by the event market, not the publisher country."""
    configured_region = (
        "korea" if article.get("region") == "korea" else "global"
    )
    if not str(category).startswith(_REGION_SPLIT_CATEGORY_PREFIXES):
        return configured_region, "configured_source_region"

    has_korea_signal = _has_any(_KOREA_EVENT_SIGNALS, text)
    has_foreign_signal = _has_any(_FOREIGN_EVENT_SIGNALS, text)
    if has_korea_signal and not has_foreign_signal:
        return "korea", "korea_event_content"
    if has_foreign_signal and not has_korea_signal:
        return "global", "foreign_event_in_korean_source"
    return configured_region, "configured_source_region"


def _matches_any_pattern(patterns: list, text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _first_pattern_group(groups: list, text: str, default: str) -> str:
    for group_name, patterns in groups:
        if _matches_any_pattern(patterns, text):
            return group_name
    return default


def _event_state(title: str, text: str) -> tuple:
    # 본문에는 과거 협상·실패 이력이 함께 나올 수 있으므로 제목의 현재 사건을
    # 먼저 판정하고, 제목만으로 불분명할 때 설명문까지 확장한다.
    event_status = _first_pattern_group(_EVENT_STATUS_PATTERNS, title, "unknown")
    if event_status == "unknown":
        event_status = _first_pattern_group(_EVENT_STATUS_PATTERNS, text, "unknown")
    reporting_basis = _first_pattern_group(
        _REPORTING_BASIS_PATTERNS,
        text,
        "unspecified",
    )
    if event_status == "unknown" and reporting_basis in {
        "sourced_report",
        "unconfirmed_rumor",
    }:
        event_status = "reported_unconfirmed"
    return event_status, reporting_basis


def _title_exclusion_reason(title: str, official_insights: bool) -> str:
    """Return a stable reason for deterministic final editorial rejection."""
    if official_insights and re.search(_OFFICIAL_PERSON_VIEW_TITLE_PATTERN, title):
        return "official_person_view"

    normalized = " ".join(title.lower().split())
    if _matches_any_pattern(_COMPOUND_ROUNDUP_PATTERNS, normalized):
        return "compound_roundup"
    if _matches_any_pattern(_TITLE_NOISE_PATTERNS, normalized):
        return "title_noise"
    if (
        not official_insights
        and _matches_any_pattern(_NON_OFFICIAL_BLOG_PATTERNS, normalized)
    ):
        return "non_official_blog"
    if (
        _matches_any_pattern(_LISTED_COMPANY_ACTION_PATTERNS, normalized)
        and not _matches_any_pattern(_CAPITAL_MARKET_POLICY_PATTERNS, normalized)
    ):
        return "listed_company_shareholder_return"
    if official_insights and _matches_any_pattern(
        _GENERIC_INSIGHT_PAGE_PATTERNS,
        normalized,
    ):
        return "generic_insight_page"
    if official_insights and _matches_any_pattern(
        _OFFICIAL_INSIGHT_NOISE_PATTERNS,
        normalized,
    ):
        return "official_profile_or_service_page"
    return ""


def _category_by_prefix(prefix: str) -> str:
    return next(category for category in CATEGORIES if category.startswith(prefix))


def _matched_groups(groups: dict, text: str) -> set:
    return {
        name
        for name, keywords in groups.items()
        if _has_any(keywords, text)
    }


def _matched_impact_themes(text: str) -> list:
    return [
        theme
        for theme, keywords in IMPACT_THEME_KEYWORDS.items()
        if _has_any(keywords, text)
    ]


def _has_specific_impact_theme(text: str) -> bool:
    """Return True when impact evidence is stronger than a broad sector word."""
    for keywords in IMPACT_THEME_KEYWORDS.values():
        specific_keywords = [
            keyword
            for keyword in keywords
            if keyword.lower() not in _BROAD_IMPACT_KEYWORDS
        ]
        if _has_any(specific_keywords, text):
            return True
    return False


def _official_aliases_for_feed(feed_name: str) -> tuple:
    """Find official publisher aliases for both direct and Google News feeds."""
    if feed_name in OFFICIAL_INSIGHTS_SOURCE_ALIASES:
        return OFFICIAL_INSIGHTS_SOURCE_ALIASES[feed_name]

    for official_feed, aliases in OFFICIAL_INSIGHTS_SOURCE_ALIASES.items():
        brand = official_feed.removesuffix(" Official Insights")
        if keyword_hit(brand, feed_name.lower()):
            return aliases
    return ()


def _is_verified_official_insight(
    feed_name: str,
    source_name: str,
    source_category: str,
    insights_category: str,
    article_link: str = "",
) -> bool:
    """Reject third-party stories that merely mention a consulting firm."""
    link_lower = article_link.lower()
    if any(domain in link_lower for domain in OFFICIAL_INSIGHTS_DOMAINS):
        return True

    if source_category != insights_category or not source_name:
        return False

    aliases = _official_aliases_for_feed(feed_name)
    return bool(aliases) and any(
        keyword_hit(alias, source_name.lower())
        for alias in aliases
    )


def summarize(article: dict):
    errors = []
    title = article.get("title", "")
    desc = article.get("description", "") or article.get("summary", "")
    text = (title + " " + desc).lower()
    raw_link = article.get("link", "")
    link = raw_link[0] if isinstance(raw_link, list) else str(raw_link or "")

    source = article.get("source", "")
    if isinstance(source, list):
        source = source[0] if source else ""
    source_clean = source.strip()

    base_score = 0.0
    feed_name = (article.get("feed") or "").strip()
    feed_override = FEED_CATEGORY_OVERRIDE.get(feed_name)
    source_meta = RSS_SOURCE_METADATA.get(feed_name) or RSS_SOURCE_METADATA.get(source_clean)
    source_category = feed_override or (source_meta or {}).get("category")
    source_priority = float((source_meta or {}).get("priority", 4.0 if feed_override else 0.0))
    base_score += source_priority

    # 출처 점수는 상대 순위의 보조 신호일 뿐이며 카테고리를 강제하지 않는다.
    category_scores = {cat: 0 for cat in CATEGORIES}
    for cat, kws in CATEGORIES.items():
        hits = sum(1 for kw in kws if keyword_hit(kw, text))
        category_scores[cat] = hits

    impact_category = _category_by_prefix("🌱")
    ai_category = _category_by_prefix("🤖")
    alternative_category = _category_by_prefix("📈")
    macro_category = _category_by_prefix("🌐")
    insights_category = _category_by_prefix("👔")

    impact_themes = _matched_impact_themes(text)
    editorial_groups = _matched_groups(EDITORIAL_PRIORITY_SIGNALS, text)
    deal_groups = _matched_groups(DEAL_PRIORITY_SIGNALS, text)
    event_status, reporting_basis = _event_state(title.lower(), text)
    early_stage = _has_any(DEAL_EARLY_STAGE_SIGNALS, text)
    non_deal_legal_event = _matches_any_pattern(
        _NON_DEAL_LEGAL_EVENT_PATTERNS,
        title.lower(),
    )
    deal_event = (
        bool(deal_groups & {"transaction", "financing"}) or early_stage
    ) and not non_deal_legal_event
    official_insights = _is_verified_official_insight(
        feed_name,
        source_clean,
        source_category,
        insights_category,
        article_link=link,
    )
    verified_impact_source = source_category == impact_category
    specific_impact_business = _has_any(_SPECIFIC_IMPACT_BUSINESS_SIGNALS, text)
    impact_content = bool(impact_themes) and (
        verified_impact_source
        or _has_specific_impact_theme(text)
        or specific_impact_business
        or _has_any(_IMPACT_PURPOSE_SIGNALS, text)
        or "impact_evidence" in editorial_groups
    )
    # 일부 구체적인 임팩트 금융·사업 표현은 기존 테마 목록에 없더라도
    # 그 자체로 충분한 임팩트 근거다.
    impact_content = impact_content or specific_impact_business
    ai_infrastructure = (
        (
            category_scores[ai_category] > 0
            or source_category == ai_category
        )
        and _has_any(_AI_INFRASTRUCTURE_SIGNALS, text)
    )
    public_market_event = _matches_any_pattern(
        _PUBLIC_MARKET_EVENT_PATTERNS,
        title.lower(),
    ) or (
        deal_event
        and _matches_any_pattern(_PRE_IPO_EVENT_PATTERNS, text)
    )
    strict_macro_content = _matches_any_pattern(_STRICT_MACRO_PATTERNS, text)

    # MBB·Big4 공식 발행물만 출처 고정. 나머지는 기사 내용을 우선한다.
    if official_insights:
        assigned_category = insights_category
        category_reason = "official_insights_source"
    elif verified_impact_source:
        assigned_category = impact_category
        category_reason = "verified_impact_source"
    elif impact_content:
        assigned_category = impact_category
        category_reason = "impact_content"
    elif public_market_event:
        assigned_category = alternative_category
        category_reason = "ipo_or_listing_event"
    elif ai_infrastructure:
        assigned_category = ai_category
        category_reason = "ai_infrastructure"
    elif deal_event:
        assigned_category = alternative_category
        category_reason = "deal_event"
    elif (
        category_scores[alternative_category] > 0
        and category_scores[alternative_category] > category_scores[ai_category]
    ):
        assigned_category = alternative_category
        category_reason = "alternative_content"
    elif category_scores[ai_category] > 0:
        assigned_category = ai_category
        category_reason = "ai_content"
    elif strict_macro_content:
        assigned_category = macro_category
        category_reason = "strict_macro_content"
    elif category_scores[alternative_category] > 0:
        assigned_category = alternative_category
        category_reason = "alternative_content"
    elif "enterprise_risk" in editorial_groups:
        assigned_category = source_category if source_category in CATEGORIES else alternative_category
        category_reason = "enterprise_risk"
    elif source_category in {
        impact_category,
        ai_category,
        alternative_category,
    }:
        assigned_category = source_category
        category_reason = "source_fallback"
    else:
        # 분명한 거시 근거가 없는 일반 비즈니스 기사를 거시에 넣지 않는다.
        assigned_category = alternative_category
        category_reason = "general_business_fallback"

    category_fit_exclusion_reason = ""
    if assigned_category == alternative_category and non_deal_legal_event:
        category_fit_exclusion_reason = "non_deal_legal_event"
    elif (
        assigned_category == alternative_category
        and "major_contract_or_technology" in editorial_groups
        and not deal_event
    ):
        # 투자·M&A·펀드가 아닌 일반 기업 계약은 대체투자 칸에 억지로
        # 넣지 않는다. 임팩트·AI·거시 근거가 있으면 앞 단계에서 이미
        # 해당 카테고리로 배정되므로 이 조건에 걸리지 않는다.
        category_fit_exclusion_reason = "contract_without_category_fit"
    elif category_reason == "general_business_fallback":
        # 카테고리 근거가 전혀 없는 일반 기업 뉴스를 대체투자의 기본값으로
        # 발송하지 않는다. Gemini가 중요한 기사로 판정하면 이후 단계에서 구제된다.
        category_fit_exclusion_reason = "general_business_without_category_fit"
    elif (
        category_reason == "enterprise_risk"
        and assigned_category == alternative_category
        and source_category != alternative_category
    ):
        category_fit_exclusion_reason = "enterprise_risk_without_investment_context"

    if assigned_category in category_scores:
        base_score += float(category_scores[assigned_category])

    for w_kw in ALL_WATCHLISTS:
        if keyword_hit(w_kw, text):
            base_score += float(WATCHLIST_WEIGHT)

    for p_kw in SOFT_PENALTY_KEYWORDS:
        if keyword_hit(p_kw, text):
            base_score -= 1.0

    editorial_score = len(editorial_groups) * EDITORIAL_PRIORITY_WEIGHT
    deal_score = len(deal_groups) * EDITORIAL_PRIORITY_WEIGHT
    impact_exception = assigned_category == impact_category and _has_any(IMPACT_EARLY_STAGE_SIGNALS, text)
    if early_stage and not deal_groups and not impact_exception:
        if "investment_or_ma" in editorial_groups:
            editorial_score -= EDITORIAL_PRIORITY_WEIGHT
        deal_score -= EDITORIAL_PRIORITY_WEIGHT

    excluded = _has_any(DEAL_EXCLUSION_KEYWORDS + EDITORIAL_EXCLUSION_KEYWORDS, text)
    if article.get("rescue_signal"):
        excluded = False
    # 최종 안전검사는 rescue 신호보다 우선한다. 행사·인터뷰·MOU·일반 목록
    # 페이지·복수 사건 종합기사는 Gemini가 실패해도 발송하지 않는다.
    title_exclusion_reason = _title_exclusion_reason(title, official_insights)
    final_exclusion_reason = title_exclusion_reason or category_fit_exclusion_reason
    if final_exclusion_reason:
        excluded = True
    base_score += editorial_score + deal_score + _EVENT_STATUS_WEIGHTS[event_status]

    article_region, region_reason = _content_region(
        article,
        assigned_category,
        text,
    )
    article["category"] = assigned_category
    article["category_reason"] = category_reason
    article["region"] = article_region
    article["region_reason"] = region_reason
    article["editorial_exclusion_reason"] = final_exclusion_reason
    article["source_priority"] = source_priority
    article["impact_themes"] = impact_themes
    article["editorial_signals"] = sorted(editorial_groups)
    article["deal_signals"] = sorted(deal_groups)
    article["event_status"] = event_status
    article["event_status_label"] = _EVENT_STATUS_LABELS[event_status]
    article["reporting_basis"] = reporting_basis
    article["reporting_basis_label"] = _REPORTING_BASIS_LABELS[reporting_basis]
    article["relevance"] = max(0.0, base_score)
    article["deal_score"] = deal_score
    article["market_signal"] = bool(editorial_groups or deal_groups) and event_status in {
        "adverse_confirmed",
        "in_progress",
        "considering",
        "reported_unconfirmed",
        "outlook",
    }
    article["major_deal"] = (
        assigned_category == alternative_category
        and bool(deal_groups & {"transaction", "financing"})
        and event_status == "confirmed"
    )
    article["impact_must_read"] = (
        assigned_category == impact_category
        and bool(
            editorial_groups
            & {
                "investment_or_ma",
                "policy_or_regulation",
                "major_contract_or_technology",
                "enterprise_risk",
                "impact_evidence",
            }
            or deal_groups
        )
    )
    article["editorial_excluded"] = excluded
    if excluded:
        article["relevance"] = 0.0
    return article, errors
