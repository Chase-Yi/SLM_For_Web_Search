from pathlib import Path

from nobodywho import Chat, Model, SamplerBuilder, SamplerConfig, TokenStream

class SLM:
    """A small language model you can talk to with .invoke(prompt).

    It wraps a nobodywho Chat, which keeps the model, the sampling rules,
    the system prompt and the conversation memory.
    """

    def __init__(
        self,
        model: Model,
        sampler: SamplerConfig,
        system_prompt: str,
        n_ctx: int,
        max_completion_tokens: int,
    ) -> None:
        """Create the chat once; then ask questions with .invoke().

        Parameters:
            model: The loaded model file — the "brain" the chat will use.
                Load it once and reuse it for every question.
            sampler: The rules for how the model picks each next word.
                For example: how random it may be (temperature)
                and what shape the output must have (JSON schema).
            system_prompt: The instruction that tells the model how to behave,
                for example: "always think step by step".
            n_ctx: How many tokens the model can keep in memory at once
                (its short-term memory size). Bigger means longer context,
                but also slower and more RAM.
            max_completion_tokens: The largest number of tokens the model is
                allowed to generate for one answer. Longer replies are cut
                off early.

        What it does:
            It builds one live chat with the model, stores it as self.chat,
            and keeps it ready for .invoke() calls.
        """
        self.max_completion_tokens = max_completion_tokens
        self.chat = Chat(
            model=model,
            sampler=sampler,
            system_prompt=system_prompt,
            n_ctx=n_ctx,
        )

    def invoke(self, user_prompt: str, chat_history: str = "") -> str:
        """Ask one question and get the model's final reply.

        Parameters:
            user_prompt: The question or message you want to ask the model.
            chat_history: Optional. The conversation so far, as one string
                (for example "User: Hi\nAssistant: Hello!"). When given, the
                chat's own history is cleared first, so this parameter is the
                only source of history.

        Returns:
            The full reply from the model, as one string.

        What it does:
            It sends the question (with any given chat history in front of
            it), collects the streamed tokens into one string, and stops
            the model early if the reply grows past max_completion_tokens.
        """
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


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parent.parent

    model_path = project_root / "model" / "Qwen3.5-0.8B-BF16.gguf"

    model = Model(model_path)

    output_schema = {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "answer": {"type": "string"},
        },
        "required": ["reasoning", "answer"],
    }

    sampler = SamplerBuilder().temperature(0.1).constrain_with_json_schema(output_schema).dist()

    system_prompt = """Always think step by step before answering.
    Write your reasoning in the "reasoning" field, 
    and your final reply in the "answer" field.
    When the Previous Conversation is provided,
    it is the only source of facts about the user.
    Remember the details the user shares (such as their name),
    and reuse them in later replies.
    If the user asks about themselves 
    (for example "Who am I?" or "What is my name?"),
    answer with the name from the Previous Conversation,
    and nothing else.
    """

    slm = SLM(
        model=model,
        sampler=sampler,
        system_prompt=system_prompt,
        n_ctx=100000,
        max_completion_tokens=1000,
    )

    user_prompt_1 = '''My name is Chase.'''

    response_1 = slm.invoke(user_prompt_1)

    print(f"User: {user_prompt_1}")
    print(f"AI: {response_1}")

    chat_history = f"User: {user_prompt_1}\nAssistant: {response_1}"

    user_prompt_2 = '''Who am I?'''

    response_2 = slm.invoke(user_prompt_2, chat_history=chat_history)

    print('\n')
    print(f"User: {user_prompt_2}")
    print(f"AI: {response_2}")
