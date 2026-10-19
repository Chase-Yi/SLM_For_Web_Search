import json
from pathlib import Path

from nobodywho import Chat, Model, SamplerBuilder, SamplerConfig, TokenStream, tool

project_root = Path(__file__).resolve().parent.parent
model_path = project_root / "model" / "Qwen3.5-0.8B-BF16.gguf"


@tool(
    description="""Gets the current price of the Nintendo Switch 2 console.
    Use this tool when the user asks about 
    the price of the Switch 2 console itself (not controllers).
    Returns the price and the store name.""",
    params={
    "store": """The region/store to check the price in.
    Use 'hk' for Hong Kong.
    """
    },
)
def get_switch2_console_price(store: str) -> str:
    """Return the mock price of the Nintendo Switch 2 console, only for Hong Kong."""
    if store != "hk":
        return """Invalid parameters. The only valid parameter is store='hk'.
    Please correct the parameters and call the tool again."""
    return f"Nintendo Switch 2 console price: HKD 3450 (store: {store})"


@tool(
    description="""Gets the current price of a Nintendo Switch 2 Pro Controller.
    Use this tool when the user asks about 
    the price of a Switch 2 Pro Controller, not the console.
    Returns the price, the controller type and the store name.""",
    params={
    "store": """The region/store to check the price in.
    Use 'hk' for Hong Kong.
    """,
    "controller_type": """The controller type to check: 
    'pro' for a Pro Controller.
    """,
    },
)
def get_switch2_controller_price(store: str, controller_type: str) -> str:
    """Return the mock price of a Nintendo Switch 2 Pro Controller, only for Hong Kong."""
    if store != "hk" or controller_type != "pro":
        return """Invalid parameters. The only valid parameters are store='hk' and controller_type='pro'.
    Please correct the parameters and call the tool again."""
    return f"Nintendo Switch 2 controller ({controller_type}) price: HKD 538 (store: {store})"


class SLM:
    """A small language model you can talk to with .invoke().

    It wraps a nobodywho Chat. Two ways to configure it:

    - pass tools (keep the default sampler): the model can call tools.
    - pass a JSON-schema sampler (no tools): the model replies in JSON.
    """

    def __init__(
        self,
        model: Model,
        system_prompt: str,
        n_ctx: int,
        max_completion_tokens: int,
        sampler: SamplerConfig | None = None,
        tools: list | None = None,
    ) -> None:
        """Create one chat; then ask questions with .invoke().

        Parameters:
            model: The loaded model file — the "brain" the chat will use.
            system_prompt: The instruction that tells the model how to behave.
            n_ctx: How many tokens the model can keep in memory at once.
            max_completion_tokens: Largest number of tokens one answer may
                have; longer replies are cut off early.
            sampler: How the model picks each next word. None = default
                (needed when tools are used). A JSON-schema sampler forces
                the output shape, but then tools must not be passed.
            tools: The tools the model may call. None = no tools.

        What it does:
            It builds one live chat, stores it as self.chat, and keeps it
            ready for .invoke() calls.
        """
        self.max_completion_tokens = max_completion_tokens
        self.chat = Chat(
            model=model,
            sampler=sampler,
            system_prompt=system_prompt,
            n_ctx=n_ctx,
            tools=tools or [],
        )

    def invoke(self, user_prompt: str, chat_history: str = "", tool_hint: str = "") -> str:
        """Ask one question and get the model's final reply.

        Parameters:
            user_prompt: The question or message you want to ask the model.
            chat_history: Optional. The conversation so far, as one string.
                When given, the chat's own history is cleared first, so this
                parameter is the only source of history.
            tool_hint: Optional instruction added after the question, with a
                blank line in between (for example "You MUST call a tool").

        Returns:
            The full reply from the model, as one string.

        What it does:
            It sends the question, collects the streamed tokens into one
            string, and stops the model early if the reply grows past
            max_completion_tokens.
        """
        if tool_hint:
            user_prompt = f"{user_prompt}\n{tool_hint}"

        if chat_history:
            self.chat.reset_history()
            user_prompt = f"Previous Conversation:\n{chat_history}\n\n{user_prompt}"

        response: TokenStream = self.chat.ask(user_prompt)

        text = ""
        for token in response:
            text += token
            if len(self.chat.tokenize(text)) - 1 >= self.max_completion_tokens:
                self.chat.stop_generation()
                break
        return text

    def build_chat_history(self) -> str:
        """Turn the chat's messages into a readable history string.

        It keeps everything, so nothing the model did is lost: user
        questions, model answers, every tool call (which tool, with which
        arguments) and every tool result (including errors).
        """
        lines = []
        for msg in self.chat.get_chat_history():
            for call in msg.get("tool_calls") or []:
                fn = call["function"]
                lines.append(
                    f"Assistant called tool {fn['name']} with arguments {fn['arguments']}"
                )

            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            if msg["role"] == "tool":
                lines.append(f"Tool {msg.get('name', 'unknown')} returned: {content}")
            else:
                lines.append(f"{msg['role'].capitalize()}: {content}")
        return "\n".join(lines)


def print_turn(index: int, question: str, chat: Chat, json_answer: str) -> None:
    """Print one turn report: the question, tool calls with results, and the JSON answer."""
    print(f"\n===== Q{index}: {question} =====")

    print("[stage 1 · tool calling]")
    for message in chat.get_chat_history():
        calls = message.get("tool_calls") or []
        for call in calls:
            fn = call["function"]
            print(f"  call: {fn['name']}({fn['arguments']})")
        if message["role"] == "assistant" and not calls:
            print("  call: no tool is being called")
        text = str(message.get("content") or "").strip()
        if not text:
            continue
        if message["role"] == "tool":
            print(f"  tool result: {text}")
        elif message["role"] == "assistant":
            print(f"  model: {text}")

    print("[stage 2 · JSON answer]")
    try:
        data = json.loads(json_answer)
    except json.JSONDecodeError:
        print(f"  raw: {json_answer}")
        return
    print(f"  reasoning: {data.get('reasoning', '')}")
    print(f"  answer: {data.get('answer', '')}")


if __name__ == "__main__":
    model = Model(model_path)

    tool_slm_sampler = SamplerBuilder().temperature(0.1).dist()

    tool_slm = SLM(
        model=model,
        system_prompt="You are a helpful assistant. Use the provided tools to answer questions.",
        n_ctx=100000,
        max_completion_tokens=1000,
        sampler=tool_slm_sampler,
        tools=[get_switch2_console_price, get_switch2_controller_price],
    )

    output_schema = {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "answer": {"type": "string"},
        },
        "required": ["reasoning", "answer"],
    }

    json_slm_sampler = SamplerBuilder().temperature(0.1).constrain_with_json_schema(output_schema).dist()

    json_system_prompt = """Always think step by step before answering.
    Write your reasoning in the "reasoning" field, and your final reply in the "answer" field.
    When the Previous Conversation is provided, must use it when answering user's question.
    """

    json_slm = SLM(
        model=model,
        system_prompt=json_system_prompt,
        n_ctx=100000,
        max_completion_tokens=1000,
        sampler=json_slm_sampler,
    )

    tool_hint = """(You MUST call one of the available tools to answer this question.
    For the console price: call get_switch2_console_price.
    For the controller price: call get_switch2_controller_price.
    Choose the tool that matches the question.)
    """

    calc_hint = """(Do NOT call any tool.
    Read the prices from the Previous Conversation 
    and Provide the total price by adding the price of each item that the user wants to buy!
    .)
    """

    turns = [
        ("What is the price of the Nintendo Switch 2 console in Hong Kong?", 
        tool_hint, False),

        ("What is the price of a Nintendo Switch 2 Pro Controller in Hong Kong?", 
        tool_hint, False),

        ("If I buy both the console and the Pro Controller, how much will they cost in total?", 
        calc_hint, True),
    ]

    chat_history = ""
    for i, (question, hint, with_history) in enumerate(turns, 1):
        tool_slm.chat.reset_history()
        history = chat_history if with_history else ""
        tool_slm.invoke(question, chat_history=history, tool_hint=hint)
        turn_history = tool_slm.build_chat_history()
        chat_history = f"{chat_history}\n{turn_history}".strip()

        json_answer = json_slm.invoke(question, chat_history=chat_history)

        print_turn(i, question, tool_slm.chat, json_answer)
