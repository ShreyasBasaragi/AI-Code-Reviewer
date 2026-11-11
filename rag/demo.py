"""Standalone, read-only demonstration of RAG retrieval.

Build the vector index separately with: python -m rag.ingest
"""
from rag.retrieve import retrieve_context

SAMPLE_CODE_SNIPPETS = [
    {
        "name": "Mutable Default Parameter Issue",
        "code": """def process_items(item_id, item_list=[]):
    item_list.append(item_id)
    return item_list""",
    },
    {
        "name": "Bare Exception Handling",
        "code": """try:
    data = fetch_external_user_data(user_id)
    save_to_database(data)
except:
    pass""",
    },
    {
        "name": "Unsafe SQL String Formatting",
        "code": """def find_user(username):
    query = f"SELECT * FROM users WHERE name = '{username}'"
    return db.execute(query)""",
    },
]


def run_demo():
    print("=" * 70)
    print("  CRITIQUE RAG MODULE - READ-ONLY RETRIEVAL DEMO")
    print("=" * 70)

    for index, sample in enumerate(SAMPLE_CODE_SNIPPETS, start=1):
        print(f"\n--- Sample {index}: {sample['name']} ---")
        print(sample["code"])
        contexts = retrieve_context(sample["code"])

        if not contexts:
            print("\n[WARN] No context returned. Check that the v2 index has been built.")
        else:
            for rank, context in enumerate(contexts, start=1):
                print(f"\n[Result #{rank}]\n{context}")

    print("\nDemo finished. No ingestion was performed.")


if __name__ == "__main__":
    run_demo()
