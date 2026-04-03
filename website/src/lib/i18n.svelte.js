// i18n.svelte.js — translations + reactive language state (Svelte 5)

export const langs = [
  { code: 'en', label: 'EN', name: 'English' },
  { code: 'es', label: 'ES', name: 'Español' },
  { code: 'ru', label: 'RU', name: 'Русский' },
  { code: 'uk', label: 'UK', name: 'Українська' },
  { code: 'ja', label: 'JA', name: '日本語' },
  { code: 'zh', label: 'ZH', name: '中文' },
  { code: 'ko', label: 'KO', name: '한국어' },
];

const STORAGE_KEY = 'pinky_lang';

function getInitialLang() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && langs.find((l) => l.code === stored)) return stored;
  } catch (_) {}
  return 'en';
}

let _currentLang = $state(getInitialLang());

export function getCurrentLang() {
  return _currentLang;
}

export function setCurrentLang(code) {
  _currentLang = code;
  try {
    localStorage.setItem(STORAGE_KEY, code);
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// Translations
// ---------------------------------------------------------------------------

const translations = {
  en: {
    // Nav
    'nav.features': 'Features',
    'nav.install': 'Install',
    'nav.docs': 'Docs',
    'nav.connect': 'Connect →',

    // Hero
    'hero.badge': 'Your personal AI agent',
    'hero.tagline': 'An AI that actually works for you.\nRemembers everything. Runs while you sleep. Batteries included.',
    'hero.pill': 'Runs on your Claude account — no API bills.',
    'hero.install': 'Install now',

    // Features section
    'features.eyebrow': 'What Pinky does',
    'features.title': 'Not a chatbot.\nYour agent.',

    'features.01.title': 'Coding Agent by Default',
    'features.01.body': 'Every agent runs Claude Code under the hood — it can read, write, and ship code out of the box. No setup, no plugins.',

    'features.02.title': 'Project Management',
    'features.02.body': 'Sprints, milestones, tasks, and burndowns — managed by your agent. It tracks what\'s in flight so you don\'t have to.',

    'features.03.title': 'Peer-Reviewed Research',
    'features.03.body': 'Agents research topics autonomously, write structured briefs, and cross-check each other\'s findings before surfacing results.',

    'features.04.title': 'Presentations',
    'features.04.body': 'Turn research into polished slide decks in one command. Share via link, password-protect, version and restore anytime.',

    'features.05.title': 'Inter-Agent Comms',
    'features.05.body': 'Agents message each other, delegate tasks, and coordinate work across sessions — like a team, not a single chatbot.',

    'features.06.title': 'Voice Chat',
    'features.06.body': 'Talk to your agent naturally. Voice in, voice out — the same persistent memory and tools, just hands-free.',

    'features.07.title': 'Memory That Just Works',
    'features.07.body': 'Built-in long-term memory with semantic search. Your agent reflects while you sleep — connecting dots, surfacing what matters.',

    'features.08.title': 'Scalable Agent Fleet',
    'features.08.body': 'Start with one agent. Spawn specialists as you need them. Each runs independently, shares context, and scales with your work.',

    'features.09.title': 'Every Channel',
    'features.09.body': 'Telegram, Slack, Discord, iMessage, SMS — your agent meets you where you are. One agent, every inbox.',

    // Install section
    'install.eyebrow': 'Installation',
    'install.title': 'On your machine in minutes.',

    'install.01.title': 'Install Claude Code',
    'install.01.body': 'Pinky runs on Claude Code. You\'ll need a Claude subscription (Pro or higher).',
    'install.01.link': 'Setup help →',

    'install.02.title': 'Install PinkyBot',
    'install.02.body': 'Clone the repo and start the daemon. It runs locally on your machine — your data stays yours.',

    'install.03.title': 'Connect and go',
    'install.03.body': 'Open the dashboard, connect your agent, and say hello. Your agent is ready — persistent memory, tools, and all.',

    // CTA
    'cta.title': 'Your agent is waiting.',
    'cta.sub': 'Connect your Pinky daemon and it gets to work — on your terms, on your machine.',
    'cta.button': 'Connect Your Agent →',

    // Theme
    'theme.light': 'light',
    'theme.dark': 'dark',

    // Footer
    'footer.text': 'pinkybot.ai · Built with Claude Code',
  },

  es: {
    'nav.features': 'Funciones',
    'nav.install': 'Instalar',
    'nav.docs': 'Docs',
    'nav.connect': 'Conectar →',

    'hero.badge': 'Tu agente de IA personal',
    'hero.tagline': 'Una IA que realmente trabaja para ti.\nRecuerda todo. Funciona mientras duermes. Todo incluido.',
    'hero.pill': 'Funciona con tu cuenta de Claude — sin costos de API.',
    'hero.install': 'Instalar ahora',

    'features.eyebrow': 'Qué hace Pinky',
    'features.title': 'No es un chatbot.\nEs tu agente.',

    'features.01.title': 'Agente de código por defecto',
    'features.01.body': 'Cada agente usa Claude Code internamente — puede leer, escribir y publicar código sin configuración ni plugins.',

    'features.02.title': 'Gestión de proyectos',
    'features.02.body': 'Sprints, hitos, tareas y avances — gestionados por tu agente. Hace el seguimiento para que tú no tengas que hacerlo.',

    'features.03.title': 'Investigación revisada por pares',
    'features.03.body': 'Los agentes investigan temas de forma autónoma, redactan informes estructurados y verifican los hallazgos entre sí antes de presentar resultados.',

    'features.04.title': 'Presentaciones',
    'features.04.body': 'Convierte investigaciones en presentaciones pulidas con un solo comando. Comparte por enlace, protege con contraseña, versiona y restaura cuando quieras.',

    'features.05.title': 'Comunicación entre agentes',
    'features.05.body': 'Los agentes se comunican, delegan tareas y coordinan trabajo entre sesiones — como un equipo, no un solo chatbot.',

    'features.06.title': 'Chat de voz',
    'features.06.body': 'Habla con tu agente de forma natural. Voz de entrada, voz de salida — la misma memoria persistente y herramientas, solo que manos libres.',

    'features.07.title': 'Memoria que simplemente funciona',
    'features.07.body': 'Memoria a largo plazo integrada con búsqueda semántica. Tu agente reflexiona mientras duermes — conectando puntos, destacando lo que importa.',

    'features.08.title': 'Flota de agentes escalable',
    'features.08.body': 'Comienza con un agente. Crea especialistas según los necesites. Cada uno funciona de forma independiente, comparte contexto y escala con tu trabajo.',

    'features.09.title': 'Todos los canales',
    'features.09.body': 'Telegram, Slack, Discord, iMessage, SMS — tu agente te encuentra donde estás. Un agente, todos los buzones.',

    'install.eyebrow': 'Instalación',
    'install.title': 'En tu máquina en minutos.',

    'install.01.title': 'Instala Claude Code',
    'install.01.body': 'Pinky funciona con Claude Code. Necesitarás una suscripción a Claude (Pro o superior).',
    'install.01.link': 'Ayuda de configuración →',

    'install.02.title': 'Instala PinkyBot',
    'install.02.body': 'Clona el repositorio e inicia el daemon. Funciona localmente en tu máquina — tus datos son tuyos.',

    'install.03.title': 'Conéctate y empieza',
    'install.03.body': 'Abre el panel, conecta tu agente y saluda. Tu agente está listo — memoria persistente, herramientas y todo lo demás.',

    'cta.title': 'Tu agente está esperando.',
    'cta.sub': 'Conecta tu daemon de Pinky y se pone a trabajar — en tus términos, en tu máquina.',
    'cta.button': 'Conectar tu agente →',

    'theme.light': 'claro',
    'theme.dark': 'oscuro',
    'footer.text': 'pinkybot.ai · Creado con Claude Code',
  },

  ru: {
    'nav.features': 'Возможности',
    'nav.install': 'Установка',
    'nav.docs': 'Docs',
    'nav.connect': 'Подключить →',

    'hero.badge': 'Твой личный ИИ-агент',
    'hero.tagline': 'ИИ, который реально работает на тебя.\nПомнит всё. Работает пока ты спишь. Всё включено.',
    'hero.pill': 'Работает с твоим аккаунтом Claude — никаких счётов за API.',
    'hero.install': 'Установить',

    'features.eyebrow': 'Что умеет Pinky',
    'features.title': 'Не чат-бот.\nТвой агент.',

    'features.01.title': 'Агент для кода по умолчанию',
    'features.01.body': 'Каждый агент работает на Claude Code — может читать, писать и публиковать код прямо из коробки. Никаких настроек, никаких плагинов.',

    'features.02.title': 'Управление проектами',
    'features.02.body': 'Спринты, этапы, задачи и прогресс — всем управляет твой агент. Он следит за тем, что происходит, чтобы ты мог не следить.',

    'features.03.title': 'Рецензируемые исследования',
    'features.03.body': 'Агенты самостоятельно исследуют темы, составляют структурированные обзоры и перепроверяют выводы друг друга перед тем, как показать результаты.',

    'features.04.title': 'Презентации',
    'features.04.body': 'Превращай исследования в отполированные слайды одной командой. Делись по ссылке, защищай паролем, управляй версиями.',

    'features.05.title': 'Общение между агентами',
    'features.05.body': 'Агенты обмениваются сообщениями, делегируют задачи и координируют работу между сессиями — как команда, а не одиночный чат-бот.',

    'features.06.title': 'Голосовой чат',
    'features.06.body': 'Общайся с агентом как обычно. Голос на входе, голос на выходе — та же постоянная память и инструменты, только руки свободны.',

    'features.07.title': 'Память, которая работает',
    'features.07.body': 'Встроенная долгосрочная память с семантическим поиском. Агент думает, пока ты спишь — связывает факты, выделяет важное.',

    'features.08.title': 'Масштабируемый флот агентов',
    'features.08.body': 'Начни с одного агента. Запускай специалистов по мере необходимости. Каждый работает независимо, делится контекстом и масштабируется вместе с задачами.',

    'features.09.title': 'Все каналы',
    'features.09.body': 'Telegram, Slack, Discord, iMessage, SMS — агент находит тебя там, где ты есть. Один агент, все входящие.',

    'install.eyebrow': 'Установка',
    'install.title': 'На твоей машине за несколько минут.',

    'install.01.title': 'Установи Claude Code',
    'install.01.body': 'Pinky работает на Claude Code. Понадобится подписка на Claude (Pro или выше).',
    'install.01.link': 'Помощь с настройкой →',

    'install.02.title': 'Установи PinkyBot',
    'install.02.body': 'Клонируй репозиторий и запусти демон. Работает локально на твоей машине — данные остаются у тебя.',

    'install.03.title': 'Подключись и начни',
    'install.03.body': 'Открой панель управления, подключи агента и поздоровайся. Агент готов — постоянная память, инструменты и всё остальное.',

    'cta.title': 'Твой агент ждёт.',
    'cta.sub': 'Подключи демон Pinky, и он приступит к работе — на твоих условиях, на твоей машине.',
    'cta.button': 'Подключить агента →',

    'theme.light': 'светлая',
    'theme.dark': 'тёмная',
    'footer.text': 'pinkybot.ai · Создано с Claude Code',
  },

  uk: {
    'nav.features': 'Можливості',
    'nav.install': 'Встановлення',
    'nav.docs': 'Docs',
    'nav.connect': 'Підключити →',

    'hero.badge': 'Твій особистий ІІ-агент',
    'hero.tagline': 'ІІ, який реально працює на тебе.\nПам\'ятає все. Працює поки ти спиш. Все включено.',
    'hero.pill': 'Працює з твоїм акаунтом Claude — жодних рахунків за API.',
    'hero.install': 'Встановити',

    'features.eyebrow': 'Що вміє Pinky',
    'features.title': 'Не чат-бот.\nТвій агент.',

    'features.01.title': 'Агент для коду за замовчуванням',
    'features.01.body': 'Кожен агент працює на Claude Code — може читати, писати та публікувати код прямо з коробки. Без налаштувань і плагінів.',

    'features.02.title': 'Управління проектами',
    'features.02.body': 'Спринти, етапи, задачі та прогрес — всім управляє твій агент. Він стежить за тим, що відбувається, щоб ти не мав цього робити.',

    'features.03.title': 'Рецензовані дослідження',
    'features.03.body': 'Агенти самостійно досліджують теми, складають структуровані огляди та перевіряють висновки один одного перед тим, як показати результати.',

    'features.04.title': 'Презентації',
    'features.04.body': 'Перетворюй дослідження на відполіровані слайди однією командою. Ділись за посиланням, захищай паролем, керуй версіями.',

    'features.05.title': 'Спілкування між агентами',
    'features.05.body': 'Агенти обмінюються повідомленнями, делегують задачі та координують роботу між сесіями — як команда, а не одиночний чат-бот.',

    'features.06.title': 'Голосовий чат',
    'features.06.body': 'Спілкуйся з агентом природно. Голос на вході, голос на виході — та сама постійна пам\'ять і інструменти, тільки руки вільні.',

    'features.07.title': 'Пам\'ять, яка просто працює',
    'features.07.body': 'Вбудована довгострокова пам\'ять із семантичним пошуком. Агент думає, поки ти спиш — зв\'язує факти, виділяє важливе.',

    'features.08.title': 'Масштабований флот агентів',
    'features.08.body': 'Починай з одного агента. Запускай спеціалістів у міру необхідності. Кожен працює незалежно, ділиться контекстом і масштабується разом із задачами.',

    'features.09.title': 'Всі канали',
    'features.09.body': 'Telegram, Slack, Discord, iMessage, SMS — агент знаходить тебе там, де ти є. Один агент, всі вхідні.',

    'install.eyebrow': 'Встановлення',
    'install.title': 'На твоїй машині за кілька хвилин.',

    'install.01.title': 'Встанови Claude Code',
    'install.01.body': 'Pinky працює на Claude Code. Знадобиться підписка на Claude (Pro або вища).',
    'install.01.link': 'Допомога з налаштуванням →',

    'install.02.title': 'Встанови PinkyBot',
    'install.02.body': 'Клонуй репозиторій і запусти демон. Працює локально на твоїй машині — дані залишаються у тебе.',

    'install.03.title': 'Підключись і починай',
    'install.03.body': 'Відкрий панель керування, підключи агента та привітайся. Агент готовий — постійна пам\'ять, інструменти та все інше.',

    'cta.title': 'Твій агент чекає.',
    'cta.sub': 'Підключи демон Pinky, і він візьметься до роботи — на твоїх умовах, на твоїй машині.',
    'cta.button': 'Підключити агента →',

    'theme.light': 'світла',
    'theme.dark': 'темна',
    'footer.text': 'pinkybot.ai · Створено з Claude Code',
  },

  ja: {
    'nav.features': '機能',
    'nav.install': 'インストール',
    'nav.docs': 'Docs',
    'nav.connect': '接続する →',

    'hero.badge': 'あなただけのAIエージェント',
    'hero.tagline': '本当にあなたのために動くAI。\nすべてを記憶し、眠っている間も働く。すぐに使える。',
    'hero.pill': 'あなたのClaudeアカウントで動作 — API料金なし。',
    'hero.install': '今すぐインストール',

    'features.eyebrow': 'Pinkyができること',
    'features.title': 'チャットボットじゃない。\nあなたのエージェント。',

    'features.01.title': 'デフォルトでコーディングエージェント',
    'features.01.body': 'すべてのエージェントはClaude Codeで動作 — セットアップやプラグイン不要で、コードの読み書き・公開ができます。',

    'features.02.title': 'プロジェクト管理',
    'features.02.body': 'スプリント、マイルストーン、タスク、進捗管理をエージェントが担当。あなたが追跡する必要はありません。',

    'features.03.title': '相互レビュー型リサーチ',
    'features.03.body': 'エージェントが自律的にトピックを調査し、構造化されたレポートを作成。結果を提示する前にお互いの発見を検証します。',

    'features.04.title': 'プレゼンテーション',
    'features.04.body': 'コマンド一つでリサーチを洗練されたスライドに変換。リンクで共有、パスワード保護、バージョン管理も自在。',

    'features.05.title': 'エージェント間コミュニケーション',
    'features.05.body': 'エージェント同士がメッセージを送り合い、タスクを委任し、セッションをまたいで作業を調整 — 一つのチャットボットではなく、チームとして機能。',

    'features.06.title': '音声チャット',
    'features.06.body': '自然に話しかけるだけ。音声入力・音声出力に対応 — 同じ記憶とツールをハンズフリーで使えます。',

    'features.07.title': 'シンプルに機能するメモリ',
    'features.07.body': 'セマンティック検索付きの長期記憶を内蔵。あなたが眠っている間もエージェントが考え、点と点をつなぎ、重要なことを浮かび上がらせます。',

    'features.08.title': 'スケーラブルなエージェント群',
    'features.08.body': '一つのエージェントから始めて、必要に応じてスペシャリストを追加。それぞれが独立して動作し、コンテキストを共有し、作業とともに拡張します。',

    'features.09.title': 'あらゆるチャンネル',
    'features.09.body': 'Telegram、Slack、Discord、iMessage、SMS — あなたがいる場所にエージェントがいます。一つのエージェント、すべての受信箱。',

    'install.eyebrow': 'インストール',
    'install.title': '数分であなたのマシンで使える。',

    'install.01.title': 'Claude Codeをインストール',
    'install.01.body': 'PinkyはClaude Codeで動作します。Claudeのサブスクリプション（Pro以上）が必要です。',
    'install.01.link': 'セットアップガイド →',

    'install.02.title': 'PinkyBotをインストール',
    'install.02.body': 'リポジトリをクローンしてデーモンを起動。あなたのマシン上でローカルに動作 — データはあなたのものです。',

    'install.03.title': '接続して始める',
    'install.03.body': 'ダッシュボードを開き、エージェントを接続して挨拶しましょう。エージェントは準備完了 — 永続メモリ、ツール、すべて揃っています。',

    'cta.title': 'あなたのエージェントが待っています。',
    'cta.sub': 'PinkyデーモンをつないだらAIはすぐに動き出します — あなたの条件で、あなたのマシンで。',
    'cta.button': 'エージェントを接続する →',

    'theme.light': 'ライト',
    'theme.dark': 'ダーク',
    'footer.text': 'pinkybot.ai · Claude Codeで構築',
  },

  zh: {
    'nav.features': '功能',
    'nav.install': '安装',
    'nav.docs': 'Docs',
    'nav.connect': '立即连接 →',

    'hero.badge': '你的个人 AI 智能体',
    'hero.tagline': '真正为你工作的 AI。\n记住一切。在你睡觉时持续运行。开箱即用。',
    'hero.pill': '运行在你的 Claude 账户上 — 无需支付 API 费用。',
    'hero.install': '立即安装',

    'features.eyebrow': 'Pinky 能做什么',
    'features.title': '不是聊天机器人。\n是你的智能体。',

    'features.01.title': '默认具备代码能力',
    'features.01.body': '每个智能体底层都运行 Claude Code — 开箱即可读取、编写和发布代码，无需任何配置或插件。',

    'features.02.title': '项目管理',
    'features.02.body': '迭代周期、里程碑、任务和进度 — 全由你的智能体管理，让你无需亲自跟进。',

    'features.03.title': '同行评审式研究',
    'features.03.body': '智能体自主研究主题，撰写结构化简报，并在呈现结果前相互交叉验证发现。',

    'features.04.title': '演示文稿',
    'features.04.body': '一条命令将研究成果转化为精美幻灯片。支持链接分享、密码保护、随时版本管理与恢复。',

    'features.05.title': '智能体间通信',
    'features.05.body': '智能体相互发送消息、委派任务，跨会话协调工作 — 像团队协作，而非单一聊天机器人。',

    'features.06.title': '语音聊天',
    'features.06.body': '与智能体自然对话。语音输入，语音输出 — 同样的持久记忆和工具，解放双手。',

    'features.07.title': '无缝记忆',
    'features.07.body': '内置具有语义搜索的长期记忆。你睡觉时智能体在思考 — 串联信息，浮现重要内容。',

    'features.08.title': '可扩展的智能体集群',
    'features.08.body': '从一个智能体开始，按需创建专家智能体。每个独立运行，共享上下文，随你的工作扩展。',

    'features.09.title': '全渠道覆盖',
    'features.09.body': 'Telegram、Slack、Discord、iMessage、SMS — 智能体在你所在的地方等你。一个智能体，所有收件箱。',

    'install.eyebrow': '安装',
    'install.title': '几分钟内在你的机器上运行。',

    'install.01.title': '安装 Claude Code',
    'install.01.body': 'Pinky 基于 Claude Code 运行，你需要 Claude 订阅（Pro 或更高级别）。',
    'install.01.link': '安装指南 →',

    'install.02.title': '安装 PinkyBot',
    'install.02.body': '克隆代码仓库并启动守护进程，本地运行在你的机器上 — 你的数据归你所有。',

    'install.03.title': '连接并开始',
    'install.03.body': '打开控制面板，连接你的智能体并打个招呼。智能体已就绪 — 持久记忆、工具，一应俱全。',

    'cta.title': '你的智能体在等你。',
    'cta.sub': '连接你的 Pinky 守护进程，它就开始工作 — 按你的方式，在你的机器上。',
    'cta.button': '连接你的智能体 →',

    'theme.light': '밝게',
    'theme.dark': '어둡게',
    'footer.text': 'pinkybot.ai · 由 Claude Code 构建',
  },

  ko: {
    'nav.features': '기능',
    'nav.install': '설치',
    'nav.docs': 'Docs',
    'nav.connect': '연결하기 →',

    'hero.badge': '나만의 개인 AI 에이전트',
    'hero.tagline': '진짜로 나를 위해 일하는 AI.\n모든 것을 기억하고, 내가 자는 동안에도 작동한다. 바로 사용 가능.',
    'hero.pill': 'Claude 계정으로 실행 — API 요금 없음.',
    'hero.install': '지금 설치하기',

    'features.eyebrow': 'Pinky가 하는 일',
    'features.title': '챗봇이 아니다.\n나의 에이전트.',

    'features.01.title': '기본 코딩 에이전트',
    'features.01.body': '모든 에이전트는 Claude Code 기반으로 동작 — 별도 설정이나 플러그인 없이 코드를 읽고, 작성하고, 배포할 수 있습니다.',

    'features.02.title': '프로젝트 관리',
    'features.02.body': '스프린트, 마일스톤, 작업, 번다운 — 에이전트가 관리합니다. 진행 상황을 직접 추적할 필요가 없습니다.',

    'features.03.title': '동료 검토 리서치',
    'features.03.body': '에이전트가 자율적으로 주제를 조사하고, 구조화된 보고서를 작성하며, 결과를 공유하기 전에 서로의 발견을 검증합니다.',

    'features.04.title': '프레젠테이션',
    'features.04.body': '명령 하나로 리서치를 세련된 슬라이드로 변환. 링크로 공유하고, 비밀번호로 보호하며, 언제든지 버전 관리와 복원이 가능합니다.',

    'features.05.title': '에이전트 간 커뮤니케이션',
    'features.05.body': '에이전트들이 서로 메시지를 주고받고, 작업을 위임하며, 세션을 넘어 협업합니다 — 하나의 챗봇이 아닌 팀처럼.',

    'features.06.title': '음성 채팅',
    'features.06.body': '에이전트와 자연스럽게 대화하세요. 음성 입력, 음성 출력 — 같은 기억과 도구를, 손을 쓰지 않고.',

    'features.07.title': '그냥 작동하는 메모리',
    'features.07.body': '시맨틱 검색이 내장된 장기 기억. 에이전트는 내가 자는 동안 생각하며 — 점들을 연결하고, 중요한 것을 떠올립니다.',

    'features.08.title': '확장 가능한 에이전트 집단',
    'features.08.body': '에이전트 하나로 시작하세요. 필요에 따라 전문가를 생성합니다. 각각 독립적으로 실행되고, 컨텍스트를 공유하며, 작업과 함께 확장됩니다.',

    'features.09.title': '모든 채널',
    'features.09.body': 'Telegram, Slack, Discord, iMessage, SMS — 에이전트는 내가 있는 곳 어디서나 만납니다. 하나의 에이전트, 모든 받은 편지함.',

    'install.eyebrow': '설치',
    'install.title': '몇 분 만에 내 기기에서 실행.',

    'install.01.title': 'Claude Code 설치',
    'install.01.body': 'Pinky는 Claude Code 기반으로 실행됩니다. Claude 구독(Pro 이상)이 필요합니다.',
    'install.01.link': '설치 가이드 →',

    'install.02.title': 'PinkyBot 설치',
    'install.02.body': '레포지토리를 클론하고 데몬을 시작하세요. 내 기기에서 로컬로 실행 — 데이터는 내 것입니다.',

    'install.03.title': '연결하고 시작하기',
    'install.03.body': '대시보드를 열고 에이전트를 연결한 후 인사를 건네세요. 에이전트가 준비되었습니다 — 지속적인 메모리, 도구, 모든 것이 갖춰져 있습니다.',

    'cta.title': '에이전트가 기다리고 있습니다.',
    'cta.sub': 'Pinky 데몬을 연결하면 바로 작동합니다 — 내 조건에 맞게, 내 기기에서.',
    'cta.button': '에이전트 연결하기 →',

    'theme.light': '라이트',
    'theme.dark': '다크',
    'footer.text': 'pinkybot.ai · Claude Code로 제작',
  },
};

export function t(key) {
  const lang = _currentLang;
  const dict = translations[lang] || translations['en'];
  return dict[key] ?? translations['en'][key] ?? key;
}
