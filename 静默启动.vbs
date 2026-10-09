Option Explicit
Dim py, fso, sh, app, ts
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("Wscript.Shell")
app = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "笔记本电源自适应.exe")
sh.CurrentDirectory = fso.GetParentFolderName(app)
sh.Run """" & app & """", 0, False
WScript.Quit 0
On Error Resume Next
Set ts = fso.OpenTextFile(fso.GetParentFolderName(app) & "\autostart_fail.log", 8, True)
ts.WriteLine Now & " launch failed"
ts.Close
