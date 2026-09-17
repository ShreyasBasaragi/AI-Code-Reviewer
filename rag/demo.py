import sys
from rag.ingest import run_ingestion
from rag.retrieve import retrieve_context

SAMPLE_CODE_SNIPPETS = [
    {
        "name": "Mutable Default Parameter Issue",
        "code": """def process_items(item_id, item_list=[]):
    item_list.append(item_id)
    return item_list"""
    },
    {
        "name": "Bare Exception Handling",
        "code": """try:
    data = fetch_external_user_data(user_id)
    save_to_database(data)
except:
    pass"""
    },
    {
        "name": "Unsafe SQL String Formatting",
        "code": """def find_user(username):
    query = f"SELECT * FROM users WHERE name = '{username}'"
    return db.execute(query)"""
    }
]


def run_demo():
    print("=" * 70)
    print("  CRITIQUE RAG MODULE - STANDALONE DEMO")
    print("=" * 70)

    # Automatically trigger ingestion if needed
    print("\n--> Verifying RAG ingestion...")
    run_ingestion()

    print("\n--> Testing Context Retrieval on Sample Code Inputs:\n")

    for i, sample in enumerate(SAMPLE_CODE_SNIPPETS, start=1):
        print(f"\n--- Sample {i}: {sample['name']} ---")
        print("Input Code:")
        print(sample['code'])
        print("\nRetrieving Top-3 Context Snippets...")

        results = retrieve_context(sample['code'], k=3)

        if not results:
            print("[WARN] No context returned.")
        else:
            for rank, context in enumerate(results, start=1):
                print(f"\n  [Result #{rank}]")
                # Indent lines for clear readable output
                indented = "\n".join("    " + line for line in context.splitlines())
                print(indented)

        print("-" * 50)

    print("\n[SUCCESS] Standalone RAG retrieval demo finished successfully!")


if __name__ == "__main__":
    run_demo()
