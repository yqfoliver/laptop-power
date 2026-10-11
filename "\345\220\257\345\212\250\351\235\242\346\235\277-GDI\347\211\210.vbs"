Option Explicit
' Switch the control panel back to the GDI engine and (re)start the program.
' Priority: --engine on the command line > panel_engine in config.json > GDI.
' The running instance is stopped first, because a second copy refuses to start.
' This file is pure ASCII on purpose: localized paths come from the file system.

Dim fso, sh, folder, file, exeName, eng
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)

If WScript.Arguments.Count > 0 Then
    eng = WScript.Arguments(0)
Else
    eng = "gdi"
End If

exeName = ""
For Each file In fso.GetFolder(folder).Files
    If LCase(fso.GetExtensionName(file.Name)) = "exe" Then
        exeName = file.Name
        Exit For
    End If
Next
If exeName = "" Then WScript.Quit 1

sh.Run "taskkill /F /T /IM """ & exeName & """", 0, True
WScript.Sleep 1500

sh.CurrentDirectory = folder
sh.Run """" & folder & "\" & exeName & """ --engine=" & eng, 0, False
WScript.Quit 0
