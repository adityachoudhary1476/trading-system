import sys

p = r'frontend/src/pages/paper/AutonomousCenter.tsx'
with open(p, 'r', encoding='utf-8') as f:
    c = f.read()

# 1. Fix title
c = c.replace('Autonomous Trading Operations Center', 'Autonomous Portfolio')

# 2. Fix subtitle
c = c.replace(
    '<span className="subtitle">Phase 6 — Paper Trading</span>',
    '<span className="subtitle">Paper-only · Bot {bot?.bot_id ?? "bot-nifty-options"}</span>',
)

# 3. Fix Stop button label
c = c.replace(
    '''            {bot && bot.state === "running" && (
              <Button
                variant="danger-solid"
                size="sm"
                disabled={actionLoading || tickLoading}
                onClick={() => handleLifecycle("stop")}
              >
                Active
              </Button>
            )}''',
    '''            {bot && bot.state === "running" && (
              <Button
                variant="danger-solid"
                size="sm"
                disabled={actionLoading || tickLoading}
                onClick={() => handleLifecycle("stop")}
              >
                Stop autonomous
              </Button>
            )}''',
)

with open(p, 'w', encoding='utf-8') as f:
    f.write(c)

print('Changes applied. Length:', len(c))
print('Title fixed:', 'Autonomous Portfolio</h1>' in c)
print('Subtitle fixed:', 'Phase 6' not in c)
print('Stop label fixed:', 'Stop autonomous</Button>' in c)
