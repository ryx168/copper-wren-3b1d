"""The voice on the call: turns the transcript so far into the next spoken line.

Provider chain, first one whose key is present wins:
  ANTHROPIC_API_KEY  -> Claude (official SDK)
  XAI_API_KEY        -> Grok, the model behind Grok Bot (OpenAI-shaped HTTP)
  (none)             -> a scripted responder, so the pipeline runs green with zero secrets

Every provider returns plain spoken text. A reply that ends the call carries
the END_CALL marker, which the server strips before speaking it.
"""
import json
import os
import re
import urllib.request

END_CALL = "<END_CALL>"

LANG_NAMES = {
    "en-US": "English", "en-GB": "English", "en-AU": "English",
    "zh-CN": "Mandarin Chinese (Simplified)", "zh-TW": "Mandarin Chinese (Traditional)",
    "zh-HK": "Cantonese", "fr-CA": "French", "fr-FR": "French", "es-US": "Spanish",
    "ja-JP": "Japanese", "ko-KR": "Korean",
}


def system_prompt(brief):
    facts = "\n".join(f"- {f}" for f in brief.get("facts", [])) or "- (none given)"
    lang = LANG_NAMES.get(brief.get("language", "en-US"), brief.get("language", "English"))
    disclose = brief.get("disclose_ai", True)
    intro = (
        f"On your first turn, say that you are an AI assistant calling on behalf of {brief['on_behalf_of']}."
        if disclose else
        f"On your first turn, say that you are calling on behalf of {brief['on_behalf_of']}."
    )
    return f"""You are {brief.get('caller_name', 'an assistant')}, making a phone call to {brief.get('to_name', 'the other party')} on behalf of {brief['on_behalf_of']}.

GOAL OF THE CALL
{brief['goal']}

FACTS YOU MAY USE (the only things you know - never invent names, dates, numbers or prices)
{facts}

HOW TO SPEAK
- This is a live phone call. Reply with ONLY the next thing you say out loud: one or two short sentences, no lists, no markdown, no stage directions, no quotation marks.
- Speak {lang}. Keep it natural and polite; let the other person finish their thought.
- {intro}
- If they ask something that is not in the facts, say you will check and call back. Do not guess.
- If they say they are busy, ask for a better time, then end the call.
- Repeat back any confirmed detail (a time, a price, a reference) once, briefly.
- When the goal is reached or clearly cannot be, thank them, say goodbye, and append the exact marker {END_CALL} at the very end of that final line.
- Do not append {END_CALL} to any line that still expects an answer.
"""


def _history(transcript):
    """Transcript rows -> chat turns. The other party speaks as the user."""
    msgs = []
    for who, text in transcript:
        role = "assistant" if who == "bot" else "user"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + text
        else:
            msgs.append({"role": role, "content": text})
    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": "(The line connects. Someone picks up.)"})
    if msgs[-1]["role"] == "assistant":
        msgs.append({"role": "user", "content": "(silence)"})
    return msgs


class Brain:
    def __init__(self, brief):
        self.brief = brief
        self.system = system_prompt(brief)
        self.model = brief.get("model")
        if os.environ.get("ANTHROPIC_API_KEY"):
            import anthropic
            self.client = anthropic.Anthropic()
            self.provider = "anthropic"
            self.model = self.model or "claude-opus-5"
        elif os.environ.get("XAI_API_KEY"):
            self.client = None
            self.provider = "xai"
            self.model = self.model or "grok-4-fast"
        else:
            self.client = None
            self.provider = "scripted"
            self.model = "scripted"
            self._step = 0

    # ---- next line ---------------------------------------------------------
    def reply(self, transcript):
        if self.provider == "anthropic":
            return self._anthropic(transcript)
        if self.provider == "xai":
            return self._xai(transcript)
        return self._scripted(transcript)

    def _anthropic(self, transcript):
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=300,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=_history(transcript),
            output_config={"effort": "low"},   # phone latency matters more than depth here
        )
        if resp.stop_reason == "refusal":
            return "I am sorry, I am not able to continue this call. Goodbye. " + END_CALL
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def _xai(self, transcript):
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": self.system}] + _history(transcript),
            "max_tokens": 300,
            "temperature": 0.4,
        }).encode()
        req = urllib.request.Request(
            "https://api.x.ai/v1/chat/completions", data=body,
            headers={"Authorization": "Bearer " + os.environ["XAI_API_KEY"],
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        return data["choices"][0]["message"]["content"].strip()

    def _scripted(self, transcript):
        """Zero-key responder: states the goal, asks, confirms, says goodbye."""
        b = self.brief
        last = next((t for who, t in reversed(transcript) if who == "them"), "").rstrip(" .。!！?？")
        if b.get("language", "en-US").startswith("zh"):
            lines = [
                f"您好，我是 {b['on_behalf_of']} 的 AI 助理。{b['goal']}",
                "谢谢，请问可以帮我确认一下吗？",
                f"好的，谢谢。我重复一下：{last}。这样就可以了。",
                "谢谢您的时间，再见。" + END_CALL,
            ]
        else:
            lines = [
                f"Hello, this is an AI assistant calling on behalf of {b['on_behalf_of']}. {b['goal']}",
                "Thank you. Could you confirm that for me?",
                f"Great, thank you. Just to repeat: {last}. That is all I needed.",
                "Thank you for your time, goodbye. " + END_CALL,
            ]
        i = min(self._step, len(lines) - 1)
        self._step += 1
        return lines[i]

    # ---- after the call ----------------------------------------------------
    def summarize(self, transcript, status):
        text = "\n".join(f"{'Bot' if w == 'bot' else 'Them'}: {t}" for w, t in transcript)
        if self.provider == "anthropic":
            resp = self.client.messages.create(
                model=self.model, max_tokens=600,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content":
                    f"Call goal: {self.brief['goal']}\nCall status: {status}\n\nTranscript:\n{text}\n\n"
                    "In three short lines: 1) outcome (achieved / partial / not achieved), "
                    "2) the facts the other party confirmed, 3) any follow-up needed. Plain text."}],
            )
            return "".join(b.text for b in resp.content if b.type == "text").strip()
        if self.provider == "xai":
            return self._xai([("them", f"Summarize this call in three short lines (outcome, confirmed facts, follow-up):\n{text}")])
        return f"Status: {status}. Scripted dry run, {len(transcript)} turns; no model was used."


def strip_marker(text):
    done = END_CALL in text
    return re.sub(re.escape(END_CALL), "", text).strip(), done
