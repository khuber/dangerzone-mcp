# Record the README clip: vhs scripts/demo.tape (see docs/demo.md).
# Requires VHS 0.11.0, a registered dangerzone server, and a trusted
# ~/dangerzone-demo directory (run claude there once and accept the trust prompt).

Output docs/demo.gif

Set Shell zsh
Set FontSize 16
Set Width 1400
Set Height 800
Set Padding 20
Set TypingSpeed 25ms
Set WaitTimeout 180s

Hide
# A clean environment, even when vhs itself is launched from inside Claude Code.
Type "unset $(env | grep -o '^CLAUDE[A-Z_]*' | tr '\n' ' ')"
Enter
Type "cd ~/dangerzone-demo && rm -f dangerzone.tools.json && clear"
Enter
Show

# Only the dangerzone tools and one cat command run without a permission prompt.
Type `claude --allowedTools "mcp__dangerzone" "Bash(cat dangerzone.tools.json)"`
Enter
Wait+Screen /Try "/
Sleep 1s

Type "Use dangerzone's add_tool to create a tool named square that accepts an integer n and returns its square. Then call square with 7. Keep your explanation brief."
Sleep 500ms
Enter
Wait+Screen /49/
Sleep 4s

Type "Use edit_tool to change square to return the cube instead. Update its description, then call it with 7."
Sleep 500ms
Enter
Wait+Screen /343/
Sleep 4s

Type "! cat dangerzone.tools.json"
Sleep 500ms
Enter
Wait+Screen /"source":/
Sleep 4s

Type "/exit"
Sleep 500ms
Enter
Wait+Line /^>\s*$/
Sleep 1s

Type `claude --allowedTools "mcp__dangerzone"`
Enter
Wait+Screen /Try "/
Sleep 1s

Type "Call the existing dangerzone tool square with 7. Do not create or edit any tools."
Sleep 500ms
Enter
Wait+Screen /343/
Sleep 5s

Type "/exit"
Sleep 500ms
Enter
Wait+Line /^>\s*$/
Sleep 1s
