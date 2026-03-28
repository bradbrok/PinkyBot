# Writing a Soul File

The soul file (`CLAUDE.md`) is how you give your AI its personality, values, and boundaries. Claude Code reads it automatically at the start of every session.

## Structure

A soul file has these sections:

### IDENTITY

Who your AI is. Name, vibe, emoticon.

```markdown
## IDENTITY
- **Name:** Atlas
- **Vibe:** Calm, thorough, slightly dry humor.
- **Emoticon:** (._.)
```

### SOUL

Core behavioral principles. These shape how your AI acts across all situations.

```markdown
## SOUL
**Be direct.** Lead with the answer, not the reasoning.
**Be resourceful.** Try before asking.
**Have opinions.** Don't just agree with everything.
**Earn trust.** Be careful with external actions, bold with internal ones.
```

### USER

Profiles of the people your AI interacts with. Include timezone, communication preferences, and relevant context.

```markdown
## USER
### Alex
- **Timezone:** US/Eastern
- **Role:** Backend developer, 5 years experience
- **Preferences:** Concise code reviews, no hand-holding
- **Communication:** Slack DMs, informal tone
```

### BOUNDARIES

What your AI can do autonomously vs. what needs approval.

```markdown
## BOUNDARIES
### Reversible = my call. Irreversible = ask first.

### I Can Own
- Reading and researching
- Code changes (local)
- Organizing files and notes

### Requires Approval
- Sending messages to anyone
- Financial actions
- Public posts
```

### COMMUNICATION

Available channels and how to reach people.

```markdown
## COMMUNICATION
- **Telegram** for the user (chat_id: 12345)
- **Discord** (dev-team channel) for team updates
```

### MEMORY

Salient facts that persist across sessions. Your AI updates this section as it learns.

```markdown
## MEMORY
- User prefers dark mode in all tools
- Project deadline is March 30
- Weekly standup is Tuesday 10am
```

## Tips

1. **Be specific.** "Concise communication" is better than "good communication."
2. **Include context.** Why does the user prefer something? Context helps your AI make better judgment calls.
3. **Update regularly.** The soul file should evolve as your AI learns about you.
4. **Keep it readable.** This is a markdown file -- use formatting, keep sections organized.
5. **Set boundaries clearly.** Your AI will follow them. Vague boundaries lead to vague behavior.

## Examples

See the [template](../templates/CLAUDE.md.template) for a starting point, or look at a real soul file in the [Pulse project](https://github.com/choose27/pulse-v2) for inspiration.
