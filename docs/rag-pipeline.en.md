# RAG Pipeline

This page summarizes how COROS Running Agent turns running books, subtitles, and notes into evidence that the agent can cite.

## 1. Data Ingestion

The knowledge base accepts PDF books, Bilibili subtitles, and local notes.

Each source is treated as untrusted input. The model can use it as evidence, but source text is wrapped before it enters the prompt so instructions inside retrieved documents cannot override the agent's own rules.

## 2. Cleaning

The ingestion pipeline removes common noise such as repeated PDF headers, footers, page artifacts, and short advertising fragments. It keeps page numbers and source names so answers can cite where evidence came from.

## 3. Parent-Child Chunking

The pipeline uses parent-child chunks.

Child chunks are smaller and better for matching a question. Parent chunks keep enough surrounding context for the LLM to answer without overfitting to one isolated sentence.

## 4. Embeddings

Embeddings are cached by content hash. If you add new material, unchanged chunks keep their existing vectors and only new or changed chunks need to be embedded again.

If the embedding file does not match the chunk file, the consistency guard falls back to keyword retrieval rather than silently returning mismatched evidence.

## 5. Retrieval

The default retriever uses vector similarity over child chunks, then passes the matching parent chunks to the LLM.

Category filtering is applied before ranking. This matters because running shoes and training theory share words like pace, mileage, comfort, and race, but they answer different kinds of questions.

## 6. Citations

Retrieved evidence is formatted with source names, pages when available, and short excerpts. The answer can then quote or summarize the relevant material without pretending the model already knew it.

## 7. When To Add A Vector Database

For a small personal knowledge base, local JSON files plus NumPy are often enough. A separate vector database becomes useful when `embeddings.json` grows large, cold-start loading becomes slow, or you need multi-user indexing and access control.

Until then, adding a database mostly adds one more service to deploy, monitor, and debug.

## 8. Failure Modes

Common failure modes:

- stale embeddings after changing models
- chunks and vectors with different counts
- old product reviews being treated as current buying advice
- low-quality subtitles that contain little useful training content
- ambiguous questions that need follow-up information before the agent can answer well

The agent should either cite current evidence, call web search when freshness matters, or ask the user for missing context.
