<#
.SYNOPSIS
    Retired. Use AIMScribeSetup.exe.

.DESCRIPTION
    This script used to install the agent by hand. It needed the PowerShell
    execution policy changed and ran unsigned on clinical PCs, which is exactly
    what Windows security is right to stop. The signed installer does the same
    job, by hand or silently:

        AIMScribeSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
            /BACKEND=https://<server> /ORIGIN=https://<cmed page> /TOKEN=<token>

    See INSTALL_FOR_IT.md beside this file.
#>
Write-Host 'install.ps1 is retired. Install with the signed AIMScribeSetup.exe - see INSTALL_FOR_IT.md.' -ForegroundColor Yellow
exit 1
