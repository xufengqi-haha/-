# 保存当前版本的快捷脚本
# 用法: .\save_version.ps1 "简短描述" "总收益金额"

param(
    [string]$Description = "更新",
    [string]$Income = "0"
)

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  保存当前版本" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 1. 备份结果文件
Write-Host "[1/4] 备份结果文件..." -ForegroundColor Yellow
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupDir = "results\backup\$timestamp"
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
Copy-Item results\monthly_income_202603.json "$backupDir\" -ErrorAction SilentlyContinue
Copy-Item results\run_summary_202603.json "$backupDir\" -ErrorAction SilentlyContinue
Write-Host "  ✓ 已备份到: $backupDir" -ForegroundColor Green
Write-Host ""

# 2. Git状态检查
Write-Host "[2/4] 检查Git状态..." -ForegroundColor Yellow
$changes = git status --porcelain
if ($changes) {
    Write-Host "  发现以下更改:" -ForegroundColor Cyan
    git status --short
    Write-Host ""
    
    # 3. 提交更改
    Write-Host "[3/4] 提交到Git..." -ForegroundColor Yellow
    git add .
    $commitMsg = "perf: $Description | 总收益¥$Income"
    git commit -m $commitMsg
    Write-Host "  ✓ 已提交: $commitMsg" -ForegroundColor Green
} else {
    Write-Host "  ℹ 没有未提交的更改" -ForegroundColor Cyan
}
Write-Host ""

# 4. 打标签
Write-Host "[4/4] 创建版本标签..." -ForegroundColor Yellow
$tagName = "v-$timestamp-income-$Income"
git tag $tagName
Write-Host "  ✓ 标签: $tagName" -ForegroundColor Green
Write-Host ""

Write-Host "========================================" -ForegroundColor Green
Write-Host "  保存完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "查看历史: git log --oneline" -ForegroundColor Cyan
Write-Host "查看标签: git tag -l" -ForegroundColor Cyan
