from rag.retrieve import retrieve_context
from llm.groq_client import GroqLLM


SAMPLE_CODE = """
def get_user_data(user_id):
    query = "SELECT * FROM users WHERE id = " + user_id
    result = database.execute(query)
    return result
""".strip()


def main() -> None:
    print("=" * 70)
    print("CRITIQUE — RAG + GROQ INTEGRATION TEST")
    print("=" * 70)

    print("\n[1] Input Code")
    print("-" * 70)
    print(SAMPLE_CODE)

    print("\n[2] Retrieving RAG Context")
    print("-" * 70)

    context = retrieve_context(SAMPLE_CODE, k=5)

    if not context:
        print("No RAG context retrieved.")
        return

    print(f"Retrieved {len(context)} context snippets:\n")

    for index, item in enumerate(context, start=1):
        print(f"--- Context #{index} ---")
        print(item)
        print()

    print("\n[3] Calling Groq")
    print("-" * 70)

    llm = GroqLLM()

    response = llm.analyze_code(
        code=SAMPLE_CODE,
        context=context,
    )

    print(response)

    print("\n" + "=" * 70)
    print("TEST COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()