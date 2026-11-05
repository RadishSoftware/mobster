"""Every way Mobster's agent can think."""

import os
import re
from dataclasses import dataclass, field, replace

from . import vault as vaults

OAI, ANTH, RESP = "openai_chat", "anthropic", "responses"


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    group: str                       # plan | key
    mechanism: str                   # key | device | local | custom
    base_url: str = ""
    wire: str = OAI
    env: tuple = ()
    model: str = ""                  # the default model, one that accepts images where the provider has one
    text_only: tuple = ()            # model-id patterns that can't see images
    all_text_only: bool = False      # every model is text-only unless the person says otherwise
    headers: dict = field(default_factory=dict)
    bearer: bool = False             # an Anthropic-wire endpoint that also wants Authorization: Bearer
    console: str = ""                # where the key comes from
    plan_label: str = ""             # "your Kimi Code plan"

    def vision(self, model):
        model = str(model or "")
        if any(re.search(pattern, model, re.I) for pattern in self.text_only):
            return False
        if self.all_text_only:
            override = (load_record(self.id) or {}).get("vision")
            return bool(override)
        return True


PLANS = (
    Provider("copilot", "GitHub Copilot", "plan", "device", "https://api.individual.githubcopilot.com", OAI, (),
             "claude-sonnet-5.5", headers={"Editor-Version": "Mobster/1", "Copilot-Vision-Request": "true",
                                           "X-Initiator": "agent", "Openai-Intent": "conversation-agent"},
             text_only=(r"mai-code-1-flash",), plan_label="your Copilot plan"),
    Provider("grok", "Grok (SuperGrok, X Premium+)", "plan", "device", "https://api.x.ai/v1", OAI, (),
             "grok-4.7", console="", plan_label="your Grok plan"),
    Provider("kimi", "Kimi Code", "plan", "key", "https://api.kimi.com/coding/v1", OAI, ("KIMI_API_KEY",),
             "kimi-for-coding", console="https://www.kimi.com/code/console", plan_label="your Kimi Code plan"),
    Provider("zai", "Z.ai GLM Coding Plan", "plan", "key", "https://api.z.ai/api/coding/paas/v4", OAI,
             ("ZAI_API_KEY",), "glm-5.3-flash", text_only=(r"^glm-(?!.*(flash|v))",),
             console="https://z.ai/manage-apikey/apikey-list", plan_label="your GLM Coding Plan"),
    Provider("minimax", "MiniMax Token Plan", "plan", "key", "https://api.minimax.io/anthropic", ANTH,
             ("MINIMAX_API_KEY",), "MiniMax-M3", text_only=(r"^minimax-m2",),
             console="https://platform.minimax.io/user-center/basic-information/interface-key",
             plan_label="your MiniMax Token Plan"),
    Provider("qwen", "Qwen (Alibaba Coding Plan)", "plan", "key", "https://coding-intl.dashscope.aliyuncs.com/v1", OAI,
             ("ALIBABA_CODING_PLAN_API_KEY",), "qwen3.7-plus",
             console="https://modelstudio.console.alibabacloud.com/", plan_label="your Alibaba Coding Plan"),
    Provider("qwen_token", "Qwen Token Plan", "plan", "key",
             "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1", OAI,
             ("QWEN_TOKEN_PLAN_API_KEY",), "qwen3.8-max", plan_label="your Qwen Token Plan"),
    Provider("mimo", "Xiaomi MiMo Token Plan", "plan", "key", "https://token-plan-sgp.xiaomimimo.com/v1", OAI,
             ("XIAOMI_TOKEN_PLAN_SGP_API_KEY",), "mimo-v2.6-pro", text_only=(r"mimo-v2\.5",),
             plan_label="your MiMo Token Plan"),
    Provider("opencode_go", "OpenCode Go", "plan", "key", "https://opencode.ai/zen/go/v1", OAI, ("OPENCODE_API_KEY",),
             "kimi-k3", console="https://opencode.ai/auth", plan_label="your OpenCode Go plan"),
)

KEYS = (
    Provider("google", "Google Gemini API", "key", "key", "https://generativelanguage.googleapis.com/v1beta/openai",
             OAI,
             ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "gemini-3.8-flash", console="https://aistudio.google.com/apikey"),
    Provider("openrouter", "OpenRouter", "key", "key", "https://openrouter.ai/api/v1", OAI, ("OPENROUTER_API_KEY",),
             "anthropic/claude-sonnet-5.5", headers={"HTTP-Referer": "https://mobster.dev", "X-Title": "Mobster"},
             console="https://openrouter.ai/settings/keys"),
    Provider("xai", "xAI", "key", "key", "https://api.x.ai/v1", OAI, ("XAI_API_KEY",), "grok-4.7",
             console="https://console.x.ai"),
    Provider("groq", "Groq", "key", "key", "https://api.groq.com/openai/v1", OAI, ("GROQ_API_KEY",), "qwen/qwen3.8-27b",
             text_only=(r"llama|gpt-oss|kimi|deepseek",), console="https://console.groq.com/keys"),
    Provider("cerebras", "Cerebras", "key", "key", "https://api.cerebras.ai/v1", OAI, ("CEREBRAS_API_KEY",),
             "qwen-3.8-27b", text_only=(r"gpt-oss|llama",), console="https://cloud.cerebras.ai"),
    Provider("mistral", "Mistral", "key", "key", "https://api.mistral.ai/v1", OAI, ("MISTRAL_API_KEY",),
             "mistral-medium-latest", text_only=(r"codestral|devstral",),
             console="https://console.mistral.ai/api-keys"),
    Provider("deepseek", "DeepSeek", "key", "key", "https://api.deepseek.com", OAI, ("DEEPSEEK_API_KEY",),
             "deepseek-flash", text_only=(r"v4-pro|reasoner",), console="https://platform.deepseek.com/api_keys"),
    Provider("together", "Together AI", "key", "key", "https://api.together.ai/v1", OAI, ("TOGETHER_API_KEY",),
             "moonshotai/Kimi-K3", console="https://api.together.ai/settings/api-keys"),
    Provider("fireworks", "Fireworks", "key", "key", "https://api.fireworks.ai/inference/v1", OAI,
             ("FIREWORKS_API_KEY",),
             "accounts/fireworks/models/kimi-k3", console="https://fireworks.ai/account/api-keys"),
    Provider("moonshot", "Moonshot (Kimi API)", "key", "key", "https://api.moonshot.ai/v1", OAI, ("MOONSHOT_API_KEY",),
             "kimi-k3", console="https://platform.moonshot.ai/console/api-keys"),
    Provider("zai_api", "Z.ai API", "key", "key", "https://api.z.ai/api/paas/v4", OAI, ("ZHIPU_API_KEY",),
             "glm-5v-turbo", text_only=(r"^glm-(?!.*(flash|v))",), console="https://z.ai/manage-apikey/apikey-list"),
    Provider("minimax_api", "MiniMax API", "key", "key", "https://api.minimax.io/anthropic", ANTH, ("MINIMAX_API_KEY",),
             "MiniMax-M3", text_only=(r"^minimax-m2",), console="https://platform.minimax.io"),
    Provider("dashscope", "Alibaba Model Studio", "key", "key",
             "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
             OAI, ("DASHSCOPE_API_KEY",), "qwen3.8-max", console="https://modelstudio.console.alibabacloud.com/"),
    Provider("xiaomi", "Xiaomi MiMo", "key", "key", "https://api.xiaomimimo.com/v1", OAI, ("XIAOMI_API_KEY",),
             "mimo-v2.6-pro", text_only=(r"mimo-v2\.5",)),
    Provider("meta", "Meta Model API", "key", "key", "https://api.meta.ai/v1", RESP, ("META_API_KEY",),
             "muse-spark-1.3", console="https://developer.meta.com/ai"),
    Provider("vercel", "Vercel AI Gateway", "key", "key", "https://ai-gateway.vercel.sh/v1", OAI,
             ("AI_GATEWAY_API_KEY",), "anthropic/claude-sonnet-5.5", console="https://vercel.com/dashboard/ai-gateway"),
    Provider("huggingface", "Hugging Face", "key", "key", "https://router.huggingface.co/v1", OAI, ("HF_TOKEN",),
             "Qwen/Qwen3.8-27B", console="https://huggingface.co/settings/tokens"),
    Provider("nvidia", "NVIDIA NIM", "key", "key", "https://integrate.api.nvidia.com/v1", OAI, ("NVIDIA_API_KEY",),
             "moonshotai/kimi-k3", text_only=(r"nemotron|llama",), console="https://build.nvidia.com"),
    Provider("baseten", "Baseten", "key", "key", "https://inference.baseten.co/v1", OAI, ("BASETEN_API_KEY",),
             "moonshotai/Kimi-K3"),
    Provider("deepinfra", "DeepInfra", "key", "key", "https://api.deepinfra.com/v1/openai", OAI, ("DEEPINFRA_API_KEY",),
             "Qwen/Qwen3.8-27B"),
    Provider("ollama", "Ollama (on this Mac)", "key", "local", "http://127.0.0.1:11434/v1", OAI, ("OLLAMA_API_KEY",),
             "qwen3-vl", all_text_only=True),
    Provider("lmstudio", "LM Studio (on this Mac)", "key", "local", "http://127.0.0.1:1234/v1", OAI,
             ("LM_STUDIO_API_KEY", "LMSTUDIO_API_KEY"), "", all_text_only=True),
    Provider("custom", "OpenAI-compatible endpoint", "key", "custom", "", OAI, (), "", all_text_only=True),
)

ALL = {p.id: p for p in PLANS + KEYS}


def get(provider_id):
    return ALL.get(provider_id)


def load_record(provider_id):
    """The saved record ({key, model, baseUrl, vision, ...}) for ``provider_id``, or {}."""
    try:
        return vaults.vault_for(f"provider-{provider_id}").load()
    except OSError:
        return {}


def save_record(provider_id, record):
    vaults.vault_for(f"provider-{provider_id}").save(record)


def forget(provider_id):
    vaults.vault_for(f"provider-{provider_id}").clear()


def credential(provider, env=None):
    """The provider's key: the saved one, else its environment variable."""
    env = os.environ if env is None else env
    record = load_record(provider.id)
    if record.get("key"):
        return record["key"]
    for name in provider.env:
        if env.get(name, "").strip():
            return env[name].strip()
    if provider.mechanism == "local" and record.get("enabled"):
        return ""
    if provider.mechanism == "device" and record.get("access_token"):
        return record["access_token"]
    return None


def base_url(provider):
    return (load_record(provider.id).get("baseUrl") or provider.base_url).rstrip("/")


def chosen_model(provider, env=None):
    env = os.environ if env is None else env
    explicit = (env.get(f"MOBSTER_{provider.id.upper()}_MODEL") or "").strip()
    return explicit or load_record(provider.id).get("model") or provider.model


def plan_name(provider):
    """The plan in a sentence's middle, "On your ___ plan": "Kimi Code", "GLM Coding", "Grok"."""
    label = provider.plan_label or provider.name
    label = label[5:] if label.startswith("your ") else label
    return label[:-5] if label.casefold().endswith(" plan") else label


def configured(provider, env=None):
    return credential(provider, env) is not None and bool(chosen_model(provider, env)) and bool(base_url(provider))


def split(model):
    """("xai", "grok-4.7") for "xai:grok-4.7"; (None, model) for any other id."""
    head, sep, rest = str(model or "").partition(":")
    return (head, rest) if sep and head in ALL else (None, model)


def model_id(provider, env=None):
    return f"{provider.id}:{chosen_model(provider, env)}"


def client(model, reasoning="low"):
    """Smart's client for "<provider>:<model>"."""
    from .compat_chat import AnthropicCompatChat, CompatChat
    provider_id, slug = split(model)
    provider = ALL[provider_id]
    key = credential(provider)
    if key is None:
        raise ValueError(f"No key for {provider.name}. Add it in Settings › Models, or run "
                         f"mobster login {provider.id}.")
    url = base_url(provider)
    spec = replace(provider, base_url=url)
    if provider.wire == RESP:
        from urllib.parse import urlsplit

        from ..model_clients import OpenAIChat
        chat = OpenAIChat(slug, key=key, reasoning=reasoning, host=urlsplit(url).netloc)
        chat.provider = provider.id
        chat.vision = provider.vision(slug)
        return chat
    token = None
    if provider.mechanism == "device":
        from . import device_login
        token = lambda: device_login.access_token(provider.id)  # noqa: E731
    cls = AnthropicCompatChat if provider.wire == ANTH else CompatChat
    return cls(spec, slug, key=key or "", token=token)


def public(env=None):
    """What the app shows of every provider: never a key, only whether one is set and from where."""
    env = os.environ if env is None else env
    from . import device_login
    out = []
    for provider in PLANS + KEYS:
        record = load_record(provider.id)
        from_env = next((name for name in provider.env if env.get(name, "").strip()), None)
        model = chosen_model(provider, env)
        out.append({"id": provider.id, "name": provider.name, "group": provider.group,
                    "mechanism": provider.mechanism, "configured": configured(provider, env),
                    "source": "saved" if record.get("key") or record.get("access_token") or record.get("enabled")
                    else ("env" if from_env else None), "envVar": provider.env[0] if provider.env else None,
                    "model": model or None, "vision": provider.vision(model) if model else None,
                    "baseUrl": base_url(provider) or None, "console": provider.console or None,
                    "planLabel": provider.plan_label or None,
                    "available": provider.mechanism != "device" or device_login.available(provider.id, env)})
    return out
