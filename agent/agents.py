import sys
from abc import ABC, abstractmethod
from pathlib import Path
import logging

from aider.coders import Coder
from aider.models import Model
from aider.io import InputOutput
import re
import os
from typing import Any, Optional
from agent.thinking_capture import ThinkingCapture

BEDROCK_REGION_MODEL_PRICING = {
    "moonshotai.kimi-k2.5": {
        "input_cost_per_token": 6e-07,
        "output_cost_per_token": 3e-06,
        "max_input_tokens": 262144,
        "max_output_tokens": 16384,
        "max_tokens": 16384,
        "mode": "chat",
        "litellm_provider": "bedrock",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_vision": True,
    },
    "zai.glm-5": {
        "input_cost_per_token": 1.2e-06,
        "output_cost_per_token": 3.84e-06,
        "max_input_tokens": 202752,
        "max_output_tokens": 128000,
        "max_tokens": 128000,
        "mode": "chat",
        "litellm_provider": "bedrock",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_vision": False,
    },
    "minimax.minimax-m2.5": {
        "input_cost_per_token": 3.6e-07,
        "output_cost_per_token": 1.44e-06,
        "max_input_tokens": 196608,
        "max_output_tokens": 8192,
        "max_tokens": 8192,
        "mode": "chat",
        "litellm_provider": "bedrock",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_vision": False,
    },
}


def register_bedrock_arn_pricing(model_name: str) -> None:
    """Register pricing for Bedrock ARN-based inference profiles in litellm's cost map.

    When using custom inference profile ARNs (e.g. bedrock/converse/arn:aws:bedrock:...),
    litellm cannot match them to known model pricing. This resolves the ARN to the
    underlying model ID and copies its pricing into the cost map under the full ARN key.
    """
    if "arn:aws:bedrock:" not in model_name:
        return

    try:
        import boto3

        region = None
        for part in model_name.split(":"):
            if (
                part.startswith("ap-")
                or part.startswith("us-")
                or part.startswith("eu-")
                or part.startswith("sa-")
            ):
                region = part
                break

        client = boto3.client("bedrock", region_name=region or "us-east-1")
        arn = model_name.split("bedrock/")[-1]
        if arn.startswith("converse/"):
            arn = arn[len("converse/") :]

        resp = client.get_inference_profile(inferenceProfileIdentifier=arn)
        models = resp.get("models", [])
        if models:
            underlying_model_id = models[0].get("modelArn", "").split("/")[-1]
            if not underlying_model_id:
                underlying_model_id = models[0].get("modelId", "")

            from aider.llm import litellm as aider_litellm
            import litellm

            for base_id, pricing in BEDROCK_REGION_MODEL_PRICING.items():
                if base_id in underlying_model_id or underlying_model_id in base_id:
                    litellm.model_cost[model_name] = pricing.copy()
                    litellm.model_cost[model_name]["litellm_provider"] = "bedrock"
                    return

            region_key = (
                f"bedrock/{region}/{underlying_model_id}"
                if region
                else f"bedrock/{underlying_model_id}"
            )
            if region_key in litellm.model_cost:
                litellm.model_cost[model_name] = litellm.model_cost[region_key].copy()
                return

            for key, val in litellm.model_cost.items():
                if underlying_model_id in key and "bedrock" in key:
                    litellm.model_cost[model_name] = val.copy()
                    return

    except Exception:
        pass


def handle_logging(logging_name: str, log_file: Path) -> None:
    """Handle logging for agent"""
    logger = logging.getLogger(logging_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger_handler = logging.FileHandler(log_file)
    logger_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(logger_handler)


class AgentReturn(ABC):
    def __init__(self, log_file: Path):
        self.log_file = log_file

        self.last_cost = 0.0


class Agents(ABC):
    def __init__(self, max_iteration: int):
        self.max_iteration = max_iteration

    @abstractmethod
    def run(self) -> AgentReturn:
        """Start agent"""
        raise NotImplementedError


class AiderReturn(AgentReturn):
    def __init__(self, log_file: Path):
        super().__init__(log_file)
        self.last_cost = self.get_money_cost()

    def get_money_cost(self) -> float:
        """Get accumulated money cost from log file"""
        last_cost = 0.0
        with open(self.log_file, "r") as file:
            for line in file:
                if "Tokens:" in line and "Cost:" in line:
                    match = re.search(
                        r"Cost: \$\d+\.\d+ message, \$(\d+\.\d+) session", line
                    )
                    if match:
                        last_cost = float(match.group(1))
        return last_cost


def _apply_thinking_capture_patches(
    coder: Any,
    thinking_capture: ThinkingCapture,
    current_stage: str,
    current_module: str,
) -> None:
    """Monkey-patch a Coder instance to capture reasoning tokens.

    Applies 4 patches that intercept reasoning content at different points
    in aider's processing pipeline, BEFORE aider strips it.
    Also patches clone() so lint_coder clones inherit the patches.
    """
    coder._thinking_capture = thinking_capture
    coder._current_stage = current_stage
    coder._current_module = current_module
    coder._turn_counter = getattr(coder, "_turn_counter", 0)
    coder._last_reasoning_content = None
    coder._last_completion_usage = None

    _original_show_send_output = coder.show_send_output
    _original_show_send_output_stream = coder.show_send_output_stream
    _original_add_assistant_reply = coder.add_assistant_reply_to_cur_messages
    _original_send_message = coder.send_message
    _original_show_usage_report = coder.show_usage_report

    coder._snapshot_prompt_tokens = 0
    coder._snapshot_completion_tokens = 0
    coder._snapshot_cost = 0.0
    coder._snapshot_cache_hit_tokens = 0
    coder._snapshot_cache_write_tokens = 0

    # Patch 1: Non-streaming response (captures reasoning_content)
    def patched_show_send_output(completion: Any) -> None:
        try:
            coder._last_reasoning_content = completion.choices[
                0
            ].message.reasoning_content
        except AttributeError:
            try:
                coder._last_reasoning_content = completion.choices[0].message.reasoning
            except AttributeError:
                coder._last_reasoning_content = None
        coder._last_completion_usage = getattr(completion, "usage", None)
        _original_show_send_output(completion)

    # Patch 2: Streaming response — intercept reasoning from chunks
    # coder.stream=True is the default; without this the non-streaming path never runs.
    # The original show_send_output_stream is a generator that builds
    # partial_response_content incrementally. We wrap the raw LLM stream
    # with an interceptor that captures reasoning while passing chunks through.

    def _reasoning_interceptor(completion: Any) -> Any:
        coder._last_reasoning_content = ""
        for chunk in completion:
            try:
                rc = chunk.choices[0].delta.reasoning_content
            except AttributeError:
                try:
                    rc = chunk.choices[0].delta.reasoning
                except AttributeError:
                    rc = None
            if rc:
                coder._last_reasoning_content += rc

            if hasattr(chunk, "usage") and chunk.usage:
                coder._last_completion_usage = chunk.usage

            yield chunk

        if not coder._last_reasoning_content:
            coder._last_reasoning_content = None

    def patched_show_send_output_stream(completion: Any) -> Any:
        return _original_show_send_output_stream(_reasoning_interceptor(completion))

    # Patch 3: User turn capture
    def patched_send_message(message: Any, *args: Any, **kwargs: Any) -> Any:
        coder._turn_counter += 1
        if coder._thinking_capture is not None:
            coder._thinking_capture.add_user_turn(
                content=message,
                stage=coder._current_stage,
                module=coder._current_module,
                turn_number=coder._turn_counter,
            )
        return _original_send_message(message, *args, **kwargs)

    # Patch 4: Assistant reply capture (with thinking + token counts)
    def patched_add_assistant_reply():
        if coder._thinking_capture is not None:
            thinking_tokens = 0
            if coder._last_completion_usage:
                thinking_tokens = (
                    getattr(coder._last_completion_usage, "reasoning_tokens", 0) or 0
                )
                if not thinking_tokens:
                    details = getattr(
                        coder._last_completion_usage,
                        "completion_tokens_details",
                        None,
                    )
                    if details and hasattr(details, "get"):
                        thinking_tokens = details.get("reasoning_tokens", 0) or 0

            coder._thinking_capture.add_assistant_turn(
                content=coder.partial_response_content,
                thinking=coder._last_reasoning_content,
                thinking_tokens=thinking_tokens,
                prompt_tokens=coder._snapshot_prompt_tokens,
                completion_tokens=coder._snapshot_completion_tokens,
                cache_hit_tokens=coder._snapshot_cache_hit_tokens,
                cache_write_tokens=coder._snapshot_cache_write_tokens,
                cost=coder._snapshot_cost,
                stage=coder._current_stage,
                module=coder._current_module,
                turn_number=coder._turn_counter,
            )
        _original_add_assistant_reply()

    # Patch 5: Propagate thinking patches to clones (used by cmd_lint)
    _original_clone = coder.clone

    # Patch 6: Snapshot tokens/cost before show_usage_report resets them
    def patched_show_usage_report() -> None:
        coder._snapshot_prompt_tokens = getattr(coder, "message_tokens_sent", 0)
        coder._snapshot_completion_tokens = getattr(coder, "message_tokens_received", 0)
        coder._snapshot_cost = getattr(coder, "message_cost", 0.0)

        usage = coder._last_completion_usage
        if usage:
            coder._snapshot_cache_hit_tokens = (
                getattr(usage, "prompt_cache_hit_tokens", 0)
                or getattr(usage, "cache_read_input_tokens", 0)
                or 0
            )
            coder._snapshot_cache_write_tokens = (
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            )

        _original_show_usage_report()

    def patched_clone(*args: Any, **kwargs: Any) -> Any:
        cloned = _original_clone(*args, **kwargs)
        _apply_thinking_capture_patches(
            cloned, thinking_capture, current_stage, current_module
        )
        cloned._turn_counter = coder._turn_counter
        return cloned

    coder.show_send_output = patched_show_send_output
    coder.show_send_output_stream = patched_show_send_output_stream
    coder.send_message = patched_send_message
    coder.add_assistant_reply_to_cur_messages = patched_add_assistant_reply
    coder.show_usage_report = patched_show_usage_report
    coder.clone = patched_clone


class AiderAgents(Agents):
    def __init__(
        self, max_iteration: int, model_name: str, cache_prompts: bool = False
    ):
        super().__init__(max_iteration)
        register_bedrock_arn_pricing(model_name)
        self._load_model_settings()
        self.model = Model(model_name)
        self.cache_prompts = cache_prompts
        # Check if API key is set for the model
        if "bedrock" in model_name:
            api_key = os.environ.get("AWS_ACCESS_KEY_ID", None) or os.environ.get(
                "AWS_BEARER_TOKEN_BEDROCK", None
            )
        elif "gpt" in model_name or "openai" in model_name:
            api_key = os.environ.get("OPENAI_API_KEY", None)
        elif "claude" in model_name:
            api_key = os.environ.get("ANTHROPIC_API_KEY", None)
        elif "gemini" in model_name:
            api_key = os.environ.get("API_KEY", None)
        else:
            raise ValueError(f"Unsupported model: {model_name}")

        if not api_key:
            raise ValueError(
                "API Key Error: There is no API key associated with the model for this agent. "
                "Edit model_name parameter in .agent.yaml, export API key for that model, and try again."
            )

    @staticmethod
    def _load_model_settings() -> None:
        from aider import models as aider_models
        from pathlib import Path

        settings_file = Path(".aider.model.settings.yml")
        if settings_file.exists():
            aider_models.register_models([str(settings_file)])

    def run(
        self,
        message: str,
        test_cmd: str,
        lint_cmd: str,
        fnames: list[str],
        log_dir: Path,
        test_first: bool = False,
        lint_first: bool = False,
        thinking_capture: Optional[ThinkingCapture] = None,
        current_stage: str = "",
        current_module: str = "",
    ) -> AgentReturn:
        """Start aider agent"""
        if test_cmd:
            auto_test = True
        else:
            auto_test = False
        if lint_cmd:
            auto_lint = True
        else:
            auto_lint = False
        log_dir = log_dir.resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        input_history_file = log_dir / ".aider.input.history"
        chat_history_file = log_dir / ".aider.chat.history.md"

        # Set up logging
        log_file = log_dir / "aider.log"
        logging.basicConfig(
            filename=log_file,
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

        # Redirect print statements to the log file
        sys.stdout = open(log_file, "a")
        sys.stderr = open(log_file, "a")

        # Configure httpx and backoff logging
        handle_logging("httpx", log_file)
        handle_logging("backoff", log_file)

        io = InputOutput(
            yes=True,
            input_history_file=input_history_file,
            chat_history_file=chat_history_file,
        )
        coder = Coder.create(
            main_model=self.model,
            fnames=fnames,
            auto_lint=auto_lint,
            auto_test=auto_test,
            lint_cmds={"python": lint_cmd},
            test_cmd=test_cmd,
            io=io,
            cache_prompts=self.cache_prompts,
        )
        coder.max_reflections = self.max_iteration
        coder.stream = True

        if thinking_capture is not None:
            _apply_thinking_capture_patches(
                coder, thinking_capture, current_stage, current_module
            )

        # Run the agent
        if test_first:
            test_errors = coder.commands.cmd_test(test_cmd)
            if test_errors:
                coder.run(test_errors)
        elif lint_first:
            coder.commands.cmd_lint(fnames=fnames)
        else:
            coder.run(message)
            # max_input = self.model.info.get("max_input_tokens", 0)
            # if max_input > 0:
            #     estimated_tokens = len(message) // 4
            #     if estimated_tokens > max_input:
            #         logger = logging.getLogger(__name__)
            #         logger.warning(
            #             f"Skipping: message ~{estimated_tokens} tokens exceeds "
            #             f"max_input_tokens {max_input} for {fnames}"
            #         )
            #         sys.stdout.close()
            #         sys.stderr.close()
            #         sys.stdout = sys.__stdout__
            #         sys.stderr = sys.__stderr__
            #         return AiderReturn(log_file)
            

        # Close redirected stdout and stderr
        sys.stdout.close()
        sys.stderr.close()
        # Restore original stdout and stderr
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__

        return AiderReturn(log_file)
