import tantivy

from config import chat_modes

# Declaring our schema
schema_builder = tantivy.SchemaBuilder()
schema_builder.add_text_field("name", stored=True)
schema_builder.add_text_field("prompt_start", stored=True)
schema = schema_builder.build()

# Creating our index (in memory)
index = tantivy.Index(schema)

writer = index.writer()
for name in chat_modes:
    writer.add_document(tantivy.Document(
        name=[name],
        prompt_start=[chat_modes[name]["prompt_start"]] if chat_modes[name]["prompt_start"] is not None else "",
    ))
# ... and committing
writer.commit()

# Reload the index to ensure it points to the last commit.
index.reload()
searcher = index.searcher()

def search_message(query_text: str) -> str:
    global index, searcher

    query = index.parse_query(query_text, ["name", "prompt_start"])
    search_rezult = searcher.search(query, 3)

    if search_rezult.count == 0:
        return "We didn't find anything"
    
    (best_score, best_doc_address) = search_rezult.hits[0]
    rezults = f"""1) {searcher.doc(best_doc_address)["name"][0]}"""
    if search_rezult.count > 1:
        (best_score, best_doc_address) = search_rezult.hits[1]
        rezults += f"""\n2) {searcher.doc(best_doc_address)["name"][0]}"""
    if search_rezult.count > 2:
        (best_score, best_doc_address) = search_rezult.hits[2]
        rezults += f"""\n3) {searcher.doc(best_doc_address)["name"][0]}"""

    return rezults