$ErrorActionPreference = "Stop"

$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

function Write-Stderr {
  param([string]$Message)
  [Console]::Error.WriteLine($Message)
}

function Get-LexicalEntry {
  param([string]$Path)

  $parent = Split-Path -Parent $Path
  $leaf = Split-Path -Leaf $Path
  if (-not [IO.Directory]::Exists($parent)) {
    return $null
  }
  return @(
    Get-ChildItem -LiteralPath $parent -Force -ErrorAction Stop |
      Where-Object { $_.Name -ieq $leaf }
  ) | Select-Object -First 1
}

function Test-ReparsePoint {
  param($Item)
  return ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
}

function Get-PythonLaunchers {
  $specifications = @(
    [PSCustomObject]@{
      Name = "py.exe"
      Arguments = @("-3", "-X", "utf8")
    },
    [PSCustomObject]@{
      Name = "python.exe"
      Arguments = @("-X", "utf8")
    }
  )

  $launchers = @()
  foreach ($specification in $specifications) {
    $command = Get-Command $specification.Name -CommandType Application -ErrorAction SilentlyContinue |
      Select-Object -First 1
    if ($null -eq $command) {
      continue
    }
    $currentDirectoryCandidate = [IO.Path]::GetFullPath(
      (Join-Path ([Environment]::CurrentDirectory) $specification.Name)
    )
    try {
      $absolute = [IO.Path]::GetFullPath([string]$command.Source)
    } catch {
      continue
    }
    if (
      $absolute.Equals($currentDirectoryCandidate, [StringComparison]::OrdinalIgnoreCase) -or
      -not [IO.File]::Exists($absolute)
    ) {
      continue
    }
    $launchers += [PSCustomObject]@{
      Path = $absolute
      Arguments = @($specification.Arguments)
    }
  }
  return $launchers
}

function Get-ConfiguredLanguage {
  param([string]$ProjectDir)

  $configScript = Join-Path $ProjectDir "bin\onevoke_config.py"
  if (-not [IO.File]::Exists($configScript)) {
    return $null
  }

  $previousNoBytecode = [Environment]::GetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "Process")
  try {
    [Environment]::SetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "1", "Process")
    foreach ($launcher in @(Get-PythonLaunchers)) {
      try {
        $result = @(
          & $launcher.Path @($launcher.Arguments) $configScript "configured-language" 2>$null
        )
      } catch {
        continue
      }
      if ($LASTEXITCODE -ne 0 -or $result.Count -eq 0) {
        continue
      }
      $language = ([string]$result[0]).Trim().ToLowerInvariant()
      if ($language -in @("cn", "en")) {
        return $language
      }
    }
  } finally {
    [Environment]::SetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", $previousNoBytecode, "Process")
  }
  return $null
}

function Show-Usage {
  param([bool]$Chinese, [bool]$ErrorStream)

  if ($Chinese) {
    $lines = @(
      "用法: install.ps1 [--lang {cn,en}]",
      "把 Onevoke 命令装到 ~/.local/bin, 规则装到 ~/.agents."
    )
  } else {
    $lines = @(
      "usage: install.ps1 [--lang {cn,en}]",
      "Install Onevoke commands to ~/.local/bin and rules to ~/.agents."
    )
  }
  foreach ($line in $lines) {
    if ($ErrorStream) {
      Write-Stderr $line
    } else {
      [Console]::Out.WriteLine($line)
    }
  }
}

function Fail-Install {
  param([string]$Message)
  throw [InvalidOperationException]::new($Message)
}

function Assert-DirectoryTarget {
  param([string]$Path, [bool]$Chinese)

  $item = Get-LexicalEntry $Path
  if ($null -eq $item) {
    return
  }
  if (-not $item.PSIsContainer) {
    if ($Chinese) {
      Fail-Install "错误: 安装目标不是目录: $Path"
    } else {
      Fail-Install "error: installation target is not a directory: $Path"
    }
  }
  if (Test-ReparsePoint $item) {
    if ($Chinese) {
      Fail-Install "错误: 安装目录不得为重解析点: $Path"
    } else {
      Fail-Install "error: installation directory must not be a reparse point: $Path"
    }
  }
}

function Assert-FileTarget {
  param([string]$Path, [bool]$Chinese, [bool]$Legacy)

  $item = Get-LexicalEntry $Path
  if ($null -eq $item) {
    return
  }
  if ($item.PSIsContainer) {
    if ($Legacy -and $Chinese) {
      Fail-Install "错误: 旧版安装目标是目录: $Path"
    } elseif ($Legacy) {
      Fail-Install "error: legacy installation target is a directory: $Path"
    } elseif ($Chinese) {
      Fail-Install "错误: 安装目标是目录: $Path"
    } else {
      Fail-Install "error: installation target is a directory: $Path"
    }
  }
  if (Test-ReparsePoint $item) {
    if ($Chinese) {
      Fail-Install "错误: 安装文件目标不得为重解析点: $Path"
    } else {
      Fail-Install "error: installation file target must not be a reparse point: $Path"
    }
  }
}

function Get-SourceFiles {
  param([string]$Directory, [string]$Extension)

  if (-not [IO.Directory]::Exists($Directory)) {
    return @()
  }
  $files = @(
    Get-ChildItem -LiteralPath $Directory -Force -File -ErrorAction Stop |
      Where-Object { [string]::IsNullOrEmpty($Extension) -or $_.Extension -ieq $Extension } |
      Sort-Object Name
  )
  return $files
}

function Test-PathEntryExists {
  param([string]$Path)
  return $null -ne (Get-LexicalEntry $Path)
}

$installArgs = @($args)
$languageSet = $false
$requestedLanguage = ""
$remainingIndex = 0
$missingLanguageValue = $false

if ($installArgs.Count -gt 0) {
  if ($installArgs[0] -eq "--lang") {
    $languageSet = $true
    if ($installArgs.Count -ge 2) {
      $requestedLanguage = [string]$installArgs[1]
      $remainingIndex = 2
    } else {
      $missingLanguageValue = $true
      $remainingIndex = 1
    }
  } elseif ([string]$installArgs[0] -like "--lang=*") {
    $languageSet = $true
    $requestedLanguage = ([string]$installArgs[0]).Substring(7)
    $remainingIndex = 1
  }
}

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$locale = ""
if ($requestedLanguage -in @("cn", "en")) {
  $locale = $requestedLanguage
}
if (-not $languageSet -or [string]::IsNullOrEmpty($locale)) {
  $locale = Get-ConfiguredLanguage $projectDir
}
if ([string]::IsNullOrEmpty($locale)) {
  foreach ($name in @("ONEVOKE_LANG", "LC_ALL", "LC_MESSAGES", "LANG")) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if (-not [string]::IsNullOrEmpty($value)) {
      $locale = $value
      break
    }
  }
}
$chinese = -not ([string]$locale -match "^(?i:en)")

if ($missingLanguageValue) {
  Show-Usage $chinese $true
  exit 2
}
if ($languageSet -and $requestedLanguage -notin @("cn", "en")) {
  Show-Usage $chinese $true
  if ($chinese) {
    Write-Stderr "错误: --lang 只接受 cn 或 en"
  } else {
    Write-Stderr "error: --lang must be cn or en"
  }
  exit 2
}

$remainingArgs = @()
if ($remainingIndex -lt $installArgs.Count) {
  $remainingArgs = @($installArgs[$remainingIndex..($installArgs.Count - 1)])
}
if ($remainingArgs.Count -eq 1 -and $remainingArgs[0] -in @("-h", "--help")) {
  Show-Usage $chinese $false
  exit 0
}
if ($remainingArgs.Count -gt 0) {
  Show-Usage $chinese $true
  exit 2
}

try {
  # Native Windows Python resolves Path.home() from USERPROFILE. Keep the
  # installer on the same boundary; HOME is commonly inherited from Git Bash
  # and may point somewhere else.
  $homeValue = [Environment]::GetEnvironmentVariable("USERPROFILE")
  if ([string]::IsNullOrWhiteSpace($homeValue)) {
    $homeValue = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
  }
  if ([string]::IsNullOrWhiteSpace($homeValue)) {
    if ($chinese) {
      Fail-Install "错误: 无法确定用户主目录"
    } else {
      Fail-Install "error: could not determine the user home directory"
    }
  }
  $userHome = [IO.Path]::GetFullPath($homeValue)

  $binDir = Join-Path $userHome ".local\bin"
  $agentsDir = Join-Path $userHome ".agents"
  $shareSource = Join-Path $projectDir "share\kanban-web"
  $shareDir = Join-Path $userHome ".local\share\onevoke\kanban-web"
  $binSource = Join-Path $projectDir "bin"
  $rulesSource = Join-Path $projectDir "rules"

  $binFiles = @(Get-SourceFiles $binSource "")
  $ruleFiles = @(Get-SourceFiles $rulesSource ".md")
  $shareFiles = @()
  if ([IO.Directory]::Exists($shareSource)) {
    $shareFiles = @(Get-SourceFiles $shareSource "")
  }

  # Preflight every managed directory and file before creating or copying anything.
  $directoryTargets = @(
    $userHome,
    (Join-Path $userHome ".local"),
    $binDir,
    $agentsDir
  )
  if ([IO.Directory]::Exists($shareSource)) {
    $directoryTargets += @(
      (Join-Path $userHome ".local\share"),
      (Join-Path $userHome ".local\share\onevoke"),
      $shareDir
    )
  }
  foreach ($directory in $directoryTargets | Select-Object -Unique) {
    Assert-DirectoryTarget $directory $chinese
  }
  foreach ($source in $binFiles) {
    Assert-FileTarget (Join-Path $binDir $source.Name) $chinese $false
  }
  foreach ($source in $ruleFiles) {
    Assert-FileTarget (Join-Path $agentsDir $source.Name) $chinese $false
  }
  foreach ($source in $shareFiles) {
    Assert-FileTarget (Join-Path $shareDir $source.Name) $chinese $false
  }

  $legacyNames = @("codex-review.sh", "claude-review.sh", "grok-review.sh")
  $legacyFound = @()
  foreach ($name in $legacyNames) {
    $target = Join-Path $binDir $name
    Assert-FileTarget $target $chinese $true
    if (Test-PathEntryExists $target) {
      $legacyFound += $name
    }
  }

  $removeLegacy = $false
  if ($legacyFound.Count -gt 0) {
    if ($chinese) {
      Write-Stderr "检测到已退役的 Reviewer 脚本:"
      Write-Stderr ("  " + ($legacyFound -join " "))
      Write-Stderr "审核入口现已统一为 onevoke-review.cmd."
      [Console]::Error.Write("是否删除这些旧脚本? [y/N] ")
    } else {
      Write-Stderr "Retired reviewer scripts were detected:"
      Write-Stderr ("  " + ($legacyFound -join " "))
      Write-Stderr "The review entry point is now unified as onevoke-review.cmd."
      [Console]::Error.Write("Delete these legacy scripts? [y/N] ")
    }
    $legacyAnswer = [Console]::In.ReadLine()
    if ([Console]::IsInputRedirected) {
      [Console]::Error.WriteLine()
    }
    if ($legacyAnswer -in @("y", "Y", "yes", "YES", "Yes", "是")) {
      $removeLegacy = $true
    } elseif ($chinese) {
      Write-Stderr "已保留旧 Reviewer 脚本."
    } else {
      Write-Stderr "Legacy reviewer scripts were kept."
    }
  }

  New-Item -ItemType Directory -Path $binDir -Force | Out-Null
  New-Item -ItemType Directory -Path $agentsDir -Force | Out-Null
  foreach ($source in $binFiles) {
    Copy-Item -LiteralPath $source.FullName -Destination (Join-Path $binDir $source.Name) -Force
  }
  foreach ($source in $ruleFiles) {
    Copy-Item -LiteralPath $source.FullName -Destination (Join-Path $agentsDir $source.Name) -Force
  }
  if ([IO.Directory]::Exists($shareSource)) {
    New-Item -ItemType Directory -Path $shareDir -Force | Out-Null
    foreach ($source in $shareFiles) {
      Copy-Item -LiteralPath $source.FullName -Destination (Join-Path $shareDir $source.Name) -Force
    }
  }

  $agentRules = Join-Path $agentsDir "AGENTS.md"
  $entryRules = Join-Path $agentsDir "ONEVOKE-AGENTS.md"
  if ([IO.File]::Exists($entryRules) -and -not (Test-PathEntryExists $agentRules)) {
    $linked = $false
    try {
      New-Item -ItemType HardLink -Path $agentRules -Target $entryRules -ErrorAction Stop | Out-Null
      $linked = $true
    } catch {
      try {
        New-Item -ItemType SymbolicLink -Path $agentRules -Target $entryRules -ErrorAction Stop | Out-Null
        $linked = $true
      } catch {
        $linked = $false
      }
    }
    if (-not $linked) {
      if ($chinese) {
        Fail-Install "错误: 无法安全创建 $agentRules; 文件系统需支持硬链接或符号链接"
      } else {
        Fail-Install "error: could not safely create $agentRules; the file system must support hard links or symbolic links"
      }
    }
  }

  if ($removeLegacy) {
    $reviewEntry = Join-Path $binDir "onevoke-review.cmd"
    $reviewItem = Get-LexicalEntry $reviewEntry
    if ($null -eq $reviewItem -or $reviewItem.PSIsContainer -or (Test-ReparsePoint $reviewItem)) {
      if ($chinese) {
        Fail-Install "错误: 新审核入口不可用, 已保留旧 Reviewer 脚本: $reviewEntry"
      } else {
        Fail-Install "error: the new review entry is unavailable; legacy reviewer scripts were kept: $reviewEntry"
      }
    }
    foreach ($name in $legacyFound) {
      [IO.File]::Delete((Join-Path $binDir $name))
    }
    if ($chinese) {
      Write-Stderr "已删除旧 Reviewer 脚本."
    } else {
      Write-Stderr "Legacy reviewer scripts were removed."
    }
  }

  $pathEntries = @($env:PATH -split ";" | ForEach-Object { $_.Trim().Trim('"').TrimEnd("\") })
  if (-not ($pathEntries -contains $binDir.TrimEnd("\"))) {
    if ($chinese) {
      Write-Stderr "提示: $binDir 不在 PATH 中. 安装器不会自动修改用户 PATH; 请手动添加并重新打开终端."
    } else {
      Write-Stderr "note: $binDir is not on PATH. The installer does not modify the user PATH; add it manually and reopen the terminal."
    }
  }

  if ($chinese) {
    [Console]::Out.WriteLine("Onevoke 已安装")
  } else {
    [Console]::Out.WriteLine("Onevoke installed")
  }

  # Installation is complete. A failed welcome is reported but never rolls files back.
  $welcomeEntry = Join-Path $binDir "onevoke.cmd"
  $welcomeArgs = @()
  if (-not [string]::IsNullOrEmpty($requestedLanguage)) {
    $welcomeArgs = @("--lang", $requestedLanguage)
  }
  $welcomeSucceeded = $false
  try {
    & $welcomeEntry @welcomeArgs "welcome"
    $welcomeSucceeded = $LASTEXITCODE -eq 0
  } catch {
    $welcomeSucceeded = $false
  }
  if (-not $welcomeSucceeded) {
    if ($chinese) {
      Write-Stderr "警告: Onevoke 文件已安装, 但 welcome 未完成; 请修复提示问题后重新运行 onevoke welcome."
      Write-Stderr "说明: MemSearch 为可选项, 其安装失败不影响本工具包; 可稍后自行安装或再跑 welcome."
    } else {
      Write-Stderr "warning: Onevoke files were installed, but welcome did not complete; fix the reported issue and rerun onevoke welcome."
      Write-Stderr "note: MemSearch is optional; installation failure does not affect this toolkit and can be retried later."
    }
  }
  exit 0
} catch {
  Write-Stderr ([string]$_.Exception.Message)
  exit 1
}
