from typing import Optional

DEFAULT_AIDER_MODEL = "openrouter/openrouter/free"


class UnsupportedEngineError(ValueError):
    pass


def build_agent_command(engine: str, model: Optional[str] = None) -> str:
    """Return a POSIX command that reads the task from $PROMPT_FILE."""
    engine_key = engine.strip().lower()
    if engine_key == "aider":
        chosen = model or DEFAULT_AIDER_MODEL
        return (
            'aider --model {model} --message "$(cat "$PROMPT_FILE")" '
            "--yes-always --no-auto-commits --no-dirty-commits --no-show-model-warnings"
        ).format(model=_posix_single_quote(chosen))
    if engine_key in ("claude-code", "claude"):
        return 'claude -p "$(cat "$PROMPT_FILE")"'
    if engine_key == "agy":
        return 'agy exec "$(cat "$PROMPT_FILE")"'
    raise UnsupportedEngineError(
        f"Unsupported agent engine '{engine}'. Use claude-code, agy, or aider."
    )


def _posix_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
