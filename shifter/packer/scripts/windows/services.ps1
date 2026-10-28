# Install services for Windows AMI (victim or DC)
# Victim: XAMPP, IIS, FTP Server, OpenSSH Server
# DC: OpenSSH Server only
#
# Usage:
#   .\services.ps1           - Victim configuration (default)
#   .\services.ps1 -Role dc  - Domain Controller configuration
#   PACKER_ROLE=dc           - Environment variable (used by Packer)
param(
    [ValidateSet("victim", "dc")]
    [string]$Role = ""
)

# If no parameter provided, check environment variable (set by Packer)
if (-not $Role) {
    $Role = if ($env:PACKER_ROLE) { $env:PACKER_ROLE } else { "victim" }
}

$ErrorActionPreference = "Stop"

Write-Host "=== Installing services (Role: $Role) ==="

if ($Role -eq "victim") {
    # ------------------------------------------------------------------------------
    # XAMPP (Apache, MySQL, PHP)
    # ------------------------------------------------------------------------------
    Write-Host "=== Installing XAMPP ==="

    $xamppVersion = "8.2.12"
    $xamppInstaller = "xampp-windows-x64-$xamppVersion-0-VS16-installer.exe"
    $xamppUrl = "https://sourceforge.net/projects/xampp/files/XAMPP%20Windows/$xamppVersion/$xamppInstaller/download"
    $xamppPath = "C:\xampp"

    # Download XAMPP
    Write-Host "Downloading XAMPP $xamppVersion..."
    $webClient = New-Object System.Net.WebClient
    $webClient.DownloadFile($xamppUrl, "C:\Windows\Temp\$xamppInstaller")

    # Install XAMPP silently
    Write-Host "Installing XAMPP..."
    $proc = Start-Process -FilePath "C:\Windows\Temp\$xamppInstaller" -ArgumentList "--mode unattended" -Wait -NoNewWindow -PassThru
    if ($proc.ExitCode -ne 0) {
        Write-Error "FATAL: XAMPP installation failed with exit code $($proc.ExitCode)"
        exit 1
    }

    # ------------------------------------------------------------------------------
    # PHP security overlay (#2556)
    # ------------------------------------------------------------------------------
    # XAMPP 8.2.12 bundles PHP 8.2.12. That version is vulnerable to php2shell
    # (CVE-2026-109618, CVE-2026-109619), and no fixed XAMPP release exists.
    # Replace the bundled PHP with the official Thread Safe build from
    # windows.php.net. Apache is not a service yet, so no PHP file is locked.
    # scripts/bake/base-image-verify.sh checks the same floor on the candidate.
    Write-Host "=== Replacing XAMPP PHP with the official PHP security release ==="

    $phpVersion = "8.2.35"
    $phpFloor = [version]"8.2.35"
    $phpZip = "php-$phpVersion-Win32-vs16-x64.zip"
    # SHA-256 from https://windows.php.net/downloads/releases/sha256sum.txt
    $phpSha256 = "CFA6000BE835931961E40740BD3B51111700061D6655DDBFE5A2125807947457"
    # A superseded release moves from releases/ to releases/archives/.
    $phpUrls = @(
        "https://windows.php.net/downloads/releases/$phpZip",
        "https://windows.php.net/downloads/releases/archives/$phpZip"
    )
    $phpZipPath = "C:\Windows\Temp\$phpZip"
    $phpStage = "C:\Windows\Temp\php-$phpVersion"

    $downloaded = $false
    foreach ($url in $phpUrls) {
        try {
            (New-Object System.Net.WebClient).DownloadFile($url, $phpZipPath)
            $downloaded = $true
            break
        } catch {
            Write-Host "PHP download failed from $url"
        }
    }
    if (-not $downloaded) {
        Write-Error "FATAL: could not download $phpZip"
        exit 1
    }

    $actualSha256 = (Get-FileHash -Path $phpZipPath -Algorithm SHA256).Hash
    if ($actualSha256 -ne $phpSha256) {
        Write-Error "FATAL: $phpZip SHA-256 is $actualSha256, expected $phpSha256"
        exit 1
    }

    # Replace the whole PHP directory, so no XAMPP 8.2.12 file stays behind.
    # Keep XAMPP's php.ini: the official archive contains none.
    $phpDir = "$xamppPath\php"
    $phpBundled = "$xamppPath\php.xampp-bundled"
    Expand-Archive -Path $phpZipPath -DestinationPath $phpStage -Force
    Copy-Item -Path "$phpDir\php.ini" -Destination "$phpStage\php.ini"
    Rename-Item -Path $phpDir -NewName (Split-Path -Leaf $phpBundled)
    Move-Item -Path $phpStage -Destination $phpDir
    Remove-Item -Path $phpZipPath -Force -ErrorAction SilentlyContinue

    # Packer runs this script over WinRM, where a native program's stderr becomes
    # an error record. Run the checks through Start-Process with file redirection.
    function Invoke-NativeCheck([string]$FilePath, [string]$Arguments) {
        $stdout = [System.IO.Path]::GetTempFileName()
        $stderr = [System.IO.Path]::GetTempFileName()
        $proc = Start-Process -FilePath $FilePath -ArgumentList $Arguments -Wait -NoNewWindow -PassThru `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        $output = "$(Get-Content -Raw $stdout)$(Get-Content -Raw $stderr)"
        Remove-Item -Path $stdout, $stderr -Force
        return @{ ExitCode = $proc.ExitCode; Output = $output }
    }

    # Fail the build when PHP does not start cleanly with XAMPP's php.ini, for
    # example when an enabled extension does not load.
    $phpExe = "$phpDir\php.exe"
    $phpStartup = Invoke-NativeCheck $phpExe "-v"
    if ($phpStartup.ExitCode -ne 0 -or $phpStartup.Output -match "Warning|Fatal|Unable to load") {
        Write-Error "FATAL: PHP did not start cleanly: $($phpStartup.Output)"
        exit 1
    }
    $installedPhp = [version](Invoke-NativeCheck $phpExe '-r "echo PHP_VERSION;"').Output.Trim()
    if ($installedPhp -lt $phpFloor) {
        Write-Error "FATAL: PHP $installedPhp is below the security floor $phpFloor"
        exit 1
    }

    # Apache loads php8apache2_4.dll while it parses its configuration.
    $httpdExe = "$xamppPath\apache\bin\httpd.exe"
    $apacheTest = Invoke-NativeCheck $httpdExe "-t"
    $apacheModules = Invoke-NativeCheck $httpdExe "-M"
    if ($apacheTest.ExitCode -ne 0 -or $apacheModules.Output -notmatch "php_module") {
        Write-Error "FATAL: Apache does not load PHP after the replacement: $($apacheTest.Output)"
        exit 1
    }

    Remove-Item -Path $phpBundled -Recurse -Force
    Write-Host "PHP $installedPhp installed (floor $phpFloor); the bundled PHP tree is removed"

    # Configure XAMPP services to start automatically
    if (Test-Path "$xamppPath\apache\bin\httpd.exe") {
        # Install Apache as a service
        Start-Process -FilePath "$xamppPath\apache\bin\httpd.exe" -ArgumentList "-k install" -Wait -NoNewWindow -ErrorAction SilentlyContinue
        Set-Service -Name "Apache2.4" -StartupType Automatic -ErrorAction SilentlyContinue
        Write-Host "Apache installed as service"
    }

    if (Test-Path "$xamppPath\mysql\bin\mysqld.exe") {
        # Install MySQL as a service
        Start-Process -FilePath "$xamppPath\mysql\bin\mysqld.exe" -ArgumentList "--install MySQL --defaults-file=`"$xamppPath\mysql\bin\my.ini`"" -Wait -NoNewWindow -ErrorAction SilentlyContinue
        Set-Service -Name "MySQL" -StartupType Automatic -ErrorAction SilentlyContinue
        Write-Host "MySQL installed as service"
    }

    # Clean up installer
    Remove-Item -Path "C:\Windows\Temp\$xamppInstaller" -Force -ErrorAction SilentlyContinue

    Write-Host "XAMPP installed to $xamppPath"

    # ------------------------------------------------------------------------------
    # IIS (Internet Information Services)
    # ------------------------------------------------------------------------------
    Write-Host "=== Installing IIS ==="

    # Install IIS with management tools
    Install-WindowsFeature -Name Web-Server -IncludeManagementTools -ErrorAction Stop
    Install-WindowsFeature -Name Web-Asp-Net45 -ErrorAction SilentlyContinue
    Install-WindowsFeature -Name Web-Net-Ext45 -ErrorAction SilentlyContinue
    Install-WindowsFeature -Name Web-ISAPI-Ext -ErrorAction SilentlyContinue
    Install-WindowsFeature -Name Web-ISAPI-Filter -ErrorAction SilentlyContinue

    # Ensure IIS service starts automatically
    Set-Service -Name "W3SVC" -StartupType Automatic

    Write-Host "IIS installed with management tools"

    # ------------------------------------------------------------------------------
    # FTP Server
    # ------------------------------------------------------------------------------
    Write-Host "=== Installing FTP Server ==="

    # Install FTP Server feature
    Install-WindowsFeature -Name Web-Ftp-Server -IncludeAllSubFeature -ErrorAction Stop

    # Configure FTP service to start automatically
    Set-Service -Name "FTPSVC" -StartupType Automatic -ErrorAction SilentlyContinue

    Write-Host "FTP Server installed"
}

# ------------------------------------------------------------------------------
# OpenSSH Server (both victim and DC)
# ------------------------------------------------------------------------------
Write-Host "=== Installing OpenSSH Server ==="

# Check if OpenSSH Server is already installed
$sshCapability = Get-WindowsCapability -Online | Where-Object { $_.Name -like "OpenSSH.Server*" }
if ($sshCapability.State -eq "Installed") {
    Write-Host "OpenSSH Server already installed"
} else {
    Write-Host "Installing OpenSSH Server capability..."
    try {
        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop
        Write-Host "OpenSSH Server capability installed via PowerShell"
    } catch {
        Write-Host "WARNING: Add-WindowsCapability failed: $($_.Exception.Message)"
        Write-Host "Attempting DISM fallback..."
        $dismResult = dism /Online /Add-Capability /CapabilityName:OpenSSH.Server~~~~0.0.1.0 /NoRestart 2>&1
        Write-Host $dismResult
    }

    # Verify installation succeeded
    $sshCapability = Get-WindowsCapability -Online | Where-Object { $_.Name -like "OpenSSH.Server*" }
    if ($sshCapability.State -ne "Installed") {
        Write-Error "FATAL: OpenSSH Server installation failed - SSH is required"
        exit 1
    }
    Write-Host "OpenSSH Server installation verified"
}

# Configure sshd service (only if it exists)
$sshdService = Get-Service -Name sshd -ErrorAction SilentlyContinue
if ($sshdService) {
    Set-Service -Name sshd -StartupType Automatic
    Write-Host "sshd service configured"
} else {
    Write-Host "WARNING: sshd service not found"
}

# Set default shell to PowerShell (create key if needed)
$openSSHKeyPath = "HKLM:\SOFTWARE\OpenSSH"
if (-not (Test-Path $openSSHKeyPath)) {
    New-Item -Path $openSSHKeyPath -Force | Out-Null
}
New-ItemProperty -Path $openSSHKeyPath -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force | Out-Null
Write-Host "Default shell set to PowerShell"

# Enable password authentication for SSH/SFTP
$sshdConfigPath = "C:\ProgramData\ssh\sshd_config"
if (Test-Path $sshdConfigPath) {
    $sshdConfig = Get-Content $sshdConfigPath
    # Enable password authentication
    $sshdConfig = $sshdConfig -replace '^#?PasswordAuthentication\s+.*', 'PasswordAuthentication yes'
    # Ensure it's added if not present
    if ($sshdConfig -notmatch 'PasswordAuthentication') {
        $sshdConfig += "`nPasswordAuthentication yes"
    }
    Set-Content -Path $sshdConfigPath -Value $sshdConfig
    Write-Host "Enabled password authentication in sshd_config"
} else {
    Write-Host "sshd_config not found (SSH may not be fully installed)"
}

# Ensure ssh-agent is available
Set-Service -Name ssh-agent -StartupType Automatic -ErrorAction SilentlyContinue

Write-Host "OpenSSH Server installed and configured"

Write-Host "=== Services installation complete ==="
