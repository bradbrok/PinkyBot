---
name: mcp-builder
description: Guide for creating high-quality MCP (Model Context Protocol) servers that enable LLMs to interact with external services through well-designed tools. Use when building MCP servers to integrate external APIs or services, whether in Python (FastMCP) or Node/TypeScript (MCP SDK).
---

# MCP Server Development Guide

Create MCP (Model Context Protocol) servers that enable LLMs to interact with external services.

## Process

### Phase 1: Research and Planning

**API Coverage vs Workflow Tools**: Balance comprehensive endpoint coverage with specialized workflow tools. Prioritize comprehensive API coverage when uncertain.

**Tool Naming**: Clear, descriptive names. Consistent prefixes (e.g., `github_create_issue`). Action-oriented.

**Recommended Stack**: TypeScript (high-quality SDK, good AI model knowledge, static typing). Streamable HTTP for remote, stdio for local.

**Study**: MCP spec at `https://modelcontextprotocol.io/sitemap.xml`, TypeScript SDK at `https://raw.githubusercontent.com/modelcontextprotocol/typescript-sdk/main/README.md`

### Phase 2: Implementation

**For each tool**:
- Input schema: Zod (TS) or Pydantic (Python) with constraints and examples
- Output schema: define `outputSchema` where possible, use `structuredContent`
- Tool description: concise summary, parameter descriptions, return type
- Annotations: `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`
- Async/await for I/O, actionable error messages, pagination support

### Phase 3: Review and Test

- No duplicated code (DRY)
- Consistent error handling
- Full type coverage
- TypeScript: `npm run build`, test with `npx @modelcontextprotocol/inspector`
- Python: `python -m py_compile your_server.py`

### Phase 4: Create Evaluations

Create 10 evaluation questions that are:
- **Independent**: Not dependent on other questions
- **Read-only**: Only non-destructive operations
- **Complex**: Multiple tool calls required
- **Realistic**: Real use cases
- **Verifiable**: Single clear answer
- **Stable**: Won't change over time

Output format:
```xml
<evaluation>
  <qa_pair>
    <question>Complex realistic question...</question>
    <answer>Specific verifiable answer</answer>
  </qa_pair>
</evaluation>
```

## Reference Files
- MCP Best Practices: `./reference/mcp_best_practices.md`
- TypeScript Guide: `./reference/node_mcp_server.md`
- Python Guide: `./reference/python_mcp_server.md`
- Evaluation Guide: `./reference/evaluation.md`