Output assets/voice-memo-agent-demo.mp4

Require tail

Set Shell "zsh"
Set FontSize 20
Set FontFamily "Menlo"
Set Width 1280
Set Height 720
Set Padding 28
Set Margin 24
Set MarginFill "#181825"
Set BorderRadius 14
Set WindowBar Colorful
Set Theme "Catppuccin Mocha"
Set LetterSpacing 0
Set LineHeight 1.2
Set TypingSpeed 35ms
Set Framerate 30
Set WaitTimeout 10m

Hide
Type "clear"
Enter
Show

Type 'tail -n 0 -F "$VOICE_MEMO_NOTES_REPO_DIR/.voice-memo-automation/agent-demo.log"'
Enter

Wait+Screen /Update committed|ready to inspect|left your notes|needs attention/
Sleep 3s
Ctrl+C
Sleep 1s
