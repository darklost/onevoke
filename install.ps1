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
  $seenPaths = @{}
  foreach ($specification in $specifications) {
    $excludedCandidates = @{}
    $currentDirectories = @([Environment]::CurrentDirectory)
    try {
      $providerDirectory = [string]$ExecutionContext.SessionState.Path.CurrentFileSystemLocation.Path
      if (-not [string]::IsNullOrWhiteSpace($providerDirectory)) {
        $currentDirectories += $providerDirectory
      }
    } catch {
      # The Win32 process directory remains the fail-safe when no FileSystem
      # provider location is available.
    }
    foreach ($currentDirectory in $currentDirectories) {
      try {
        $candidate = [IO.Path]::GetFullPath(
          (Join-Path $currentDirectory $specification.Name)
        )
        $excludedCandidates[$candidate.ToLowerInvariant()] = $true
      } catch {
        continue
      }
    }
    $commands = @(
      Get-Command $specification.Name -CommandType Application -All -ErrorAction SilentlyContinue
    )
    foreach ($command in $commands) {
      try {
        $absolute = [IO.Path]::GetFullPath([string]$command.Source)
      } catch {
        continue
      }
      $pathKey = $absolute.ToLowerInvariant()
      if (
        $excludedCandidates.ContainsKey($pathKey) -or
        -not [IO.File]::Exists($absolute) -or
        $seenPaths.ContainsKey($pathKey)
      ) {
        continue
      }
      $seenPaths[$pathKey] = $true
      $launchers += [PSCustomObject]@{
        Path = $absolute
        Arguments = @($specification.Arguments)
      }
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
      "用法: install.ps1 [--lang {cn,en}] [--project <目录>]",
      "无参数时把 Onevoke 命令装到 ~/.local/bin, 规则装到 ~/.agents.",
      "--project 把载荷装到目标 Git 项目主 worktree 的 .onevoke/, 完全跳过全局安装."
    )
  } else {
    $lines = @(
      "usage: install.ps1 [--lang {cn,en}] [--project <directory>]",
      "With no arguments, install Onevoke commands to ~/.local/bin and rules to ~/.agents.",
      "--project installs into the target Git main worktree .onevoke/ and skips global paths."
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

function Invoke-PythonConfigCommand {
  param(
    [string]$SourceRoot,
    [string[]]$Arguments,
    [bool]$Chinese
  )

  $configScript = Join-Path $SourceRoot "bin\onevoke_config.py"
  if (-not [IO.File]::Exists($configScript)) {
    if ($Chinese) {
      Fail-Install "错误: 找不到配置脚本: $configScript"
    } else {
      Fail-Install "error: configuration script not found: $configScript"
    }
  }

  $launchers = @(Get-PythonLaunchers)
  if ($launchers.Count -eq 0) {
    if ($Chinese) {
      Fail-Install "错误: 未找到可用的 Python 3"
    } else {
      Fail-Install "error: no usable Python 3 was found"
    }
  }

  $previousNoBytecode = [Environment]::GetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "Process")
  $previousErrorAction = $ErrorActionPreference
  try {
    [Environment]::SetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "1", "Process")
    foreach ($launcher in $launchers) {
      $ErrorActionPreference = "Continue"
      try {
        $output = & $launcher.Path @($launcher.Arguments) $configScript @Arguments 2>&1
        $code = $LASTEXITCODE
      } catch {
        continue
      } finally {
        $ErrorActionPreference = $previousErrorAction
      }
      $stdoutLines = @()
      $stderrLines = @()
      foreach ($item in @($output)) {
        if ($item -is [System.Management.Automation.ErrorRecord]) {
          $stderrLines += [string]$item
        } else {
          $stdoutLines += [string]$item
        }
      }
      $stdout = [string]::Join("`n", $stdoutLines).Trim()
      $stderr = [string]::Join("`n", $stderrLines).Trim()
      if ($code -ne 0) {
        if (-not [string]::IsNullOrWhiteSpace($stderr)) {
          Fail-Install $stderr
        }
        if (-not [string]::IsNullOrWhiteSpace($stdout)) {
          Fail-Install $stdout
        }
        if ($Chinese) {
          Fail-Install "错误: 无法准备项目安装路径"
        } else {
          Fail-Install "error: failed to prepare project install paths"
        }
      }
      return $stdout
    }
  } finally {
    [Environment]::SetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", $previousNoBytecode, "Process")
    $ErrorActionPreference = $previousErrorAction
  }
  if ($Chinese) {
    Fail-Install "错误: 未找到可用的 Python 3"
  } else {
    Fail-Install "error: no usable Python 3 was found"
  }
}

function Convert-InstallPathsJson {
  param([string]$Json, [bool]$Chinese)

  try {
    $paths = $Json | ConvertFrom-Json
  } catch {
    if ($Chinese) {
      Fail-Install "错误: 无法解析项目安装路径"
    } else {
      Fail-Install "error: failed to parse project install paths"
    }
  }
  foreach ($name in @("project_root", "install_root", "bin_dir", "rules_dir", "share_dir")) {
    $value = [string]$paths.$name
    if ([string]::IsNullOrWhiteSpace($value)) {
      if ($Chinese) {
        Fail-Install "错误: 项目安装路径缺少 $name"
      } else {
        Fail-Install "error: project install paths are missing $name"
      }
    }
  }
  return $paths
}

$installArgs = @($args)
$languageSet = $false
$requestedLanguage = ""
$missingLanguageValue = $false
$projectSet = $false
$projectTarget = ""
$missingProjectValue = $false
$showHelp = $false
$unknownArgs = @()

$argIndex = 0
while ($argIndex -lt $installArgs.Count) {
  $current = [string]$installArgs[$argIndex]
  if ($current -eq "--lang") {
    $languageSet = $true
    if (($argIndex + 1) -ge $installArgs.Count) {
      $missingLanguageValue = $true
      $argIndex += 1
      continue
    }
    $requestedLanguage = [string]$installArgs[$argIndex + 1]
    $argIndex += 2
    continue
  }
  if ($current -like "--lang=*") {
    $languageSet = $true
    $requestedLanguage = $current.Substring(7)
    $argIndex += 1
    continue
  }
  if ($current -eq "--project") {
    $projectSet = $true
    if (($argIndex + 1) -ge $installArgs.Count) {
      $missingProjectValue = $true
      $argIndex += 1
      continue
    }
    $projectTarget = [string]$installArgs[$argIndex + 1]
    $argIndex += 2
    continue
  }
  if ($current -like "--project=*") {
    $projectSet = $true
    $projectTarget = $current.Substring(10)
    $argIndex += 1
    continue
  }
  if ($current -in @("-h", "--help")) {
    $showHelp = $true
    $argIndex += 1
    continue
  }
  $unknownArgs += $current
  $argIndex += 1
}
if ($projectSet -and [string]::IsNullOrWhiteSpace($projectTarget)) {
  $missingProjectValue = $true
}

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$locale = ""
if ($requestedLanguage -in @("cn", "en")) {
  $locale = $requestedLanguage
}
# 项目安装不得探测全局 USERPROFILE/HOME 下的 Onevoke 配置.
if ((-not $languageSet -or [string]::IsNullOrEmpty($locale)) -and -not $projectSet) {
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

if ($missingLanguageValue -or $missingProjectValue -or $unknownArgs.Count -gt 0) {
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
if ($showHelp) {
  Show-Usage $chinese $false
  exit 0
}

try {
  $shareSource = Join-Path $projectDir "share\kanban-web"
  $binSource = Join-Path $projectDir "bin"
  $rulesSource = Join-Path $projectDir "rules"

  if ($projectSet) {
    $preparedJson = Invoke-PythonConfigCommand `
      $projectDir `
      @("prepare-project-install", $projectTarget) `
      $chinese
    $installPaths = Convert-InstallPathsJson $preparedJson $chinese
    $binDir = [string]$installPaths.bin_dir
    $agentsDir = [string]$installPaths.rules_dir
    $shareDir = Join-Path ([string]$installPaths.share_dir) "kanban-web"
    $installRoot = [string]$installPaths.install_root

    $binFiles = @(Get-SourceFiles $binSource "")
    $ruleFiles = @(Get-SourceFiles $rulesSource ".md")
    $shareFiles = @()
    if ([IO.Directory]::Exists($shareSource)) {
      $shareFiles = @(Get-SourceFiles $shareSource "")
    }

    $directoryTargets = @(
      $installRoot,
      $binDir,
      $agentsDir
    )
    if ([IO.Directory]::Exists($shareSource)) {
      $directoryTargets += @(([string]$installPaths.share_dir), $shareDir)
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

    foreach ($source in $binFiles) {
      Copy-Item -LiteralPath $source.FullName -Destination (Join-Path $binDir $source.Name) -Force
    }
    foreach ($source in $ruleFiles) {
      Copy-Item -LiteralPath $source.FullName -Destination (Join-Path $agentsDir $source.Name) -Force
    }
    if ([IO.Directory]::Exists($shareSource)) {
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

    $onevokeCommand = Join-Path $binDir "onevoke.cmd"
    $kanbanCommand = Join-Path $binDir "kanban.cmd"
    if ($chinese) {
      Write-Stderr "项目命令: $onevokeCommand"
      Write-Stderr "看板命令: $kanbanCommand"
      Write-Stderr "请使用上述绝对路径; 不要把项目命令根加入 PATH, 也不要使用全局同名命令."
      [Console]::Out.WriteLine("Onevoke 已安装")
    } else {
      Write-Stderr "project command: $onevokeCommand"
      Write-Stderr "kanban command: $kanbanCommand"
      Write-Stderr "Use these absolute paths; do not add the project command root to PATH or use global commands of the same name."
      [Console]::Out.WriteLine("Onevoke installed")
    }

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
  }

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
  $shareDir = Join-Path $userHome ".local\share\onevoke\kanban-web"

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
