# Parse only. Never invoke, dot-source, or evaluate the submitted text.
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$sourceText = [Console]::In.ReadToEnd()
$parseTokens = $null
$parseErrors = $null
$syntaxTree = [System.Management.Automation.Language.Parser]::ParseInput($sourceText, [ref]$parseTokens, [ref]$parseErrors)
$allowedNodes = @('ScriptBlockAst','NamedBlockAst','PipelineAst','CommandAst','CommandParameterAst',
  'CommandExpressionAst','StringConstantExpressionAst','ConstantExpressionAst','ArrayLiteralAst',
  'ParenExpressionAst','MemberExpressionAst','BinaryExpressionAst','UnaryExpressionAst',
  'ExpandableStringExpressionAst','SubExpressionAst','StatementBlockAst','VariableExpressionAst')
$allNodes = @($syntaxTree.FindAll({ param($node) $true }, $true))
$validSyntax = $parseErrors.Count -eq 0
foreach ($node in $allNodes) {
  if ($node.GetType().Name -notin $allowedNodes) { $validSyntax = $false }
  if ($node -is [System.Management.Automation.Language.VariableExpressionAst] -and
      $node.VariablePath.UserPath -notin @('true','false','null')) { $validSyntax = $false }
  if ($node -is [System.Management.Automation.Language.CommandAst] -and
      $node.InvocationOperator.ToString() -ne 'Unknown') { $validSyntax = $false }
  if ($node -is [System.Management.Automation.Language.UnaryExpressionAst] -and
      $node.TokenKind.ToString() -notin @('Not','Exclaim','Minus','Plus','Comma','Bnot','Join','Split')) { $validSyntax = $false }
}
function Describe-Literal($node) {
  if ($node -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
      $node -is [System.Management.Automation.Language.ConstantExpressionAst]) {
    return @{kind='literal'; values=@([string]$node.Value)}
  }
  if ($node -is [System.Management.Automation.Language.ArrayLiteralAst]) {
    $items = @()
    foreach ($element in $node.Elements) {
      $item = Describe-Literal $element
      if ($item.kind -ne 'literal') { return @{kind='dynamic'; values=@()} }
      $items += $item.values
    }
    return @{kind='literal'; values=$items}
  }
  return @{kind='dynamic'; values=@()}
}
$commands = @($allNodes | Where-Object { $_ -is [System.Management.Automation.Language.CommandAst] } | ForEach-Object {
  $commandNode = $_
  $elements = @($commandNode.CommandElements | Select-Object -Skip 1 | ForEach-Object {
    if ($_ -is [System.Management.Automation.Language.CommandParameterAst]) {
      @{kind='parameter'; name=$_.ParameterName; argument=$(if ($_.Argument) { Describe-Literal $_.Argument } else { $null })}
    } else { Describe-Literal $_ }
  })
  @{name=$commandNode.GetCommandName(); elements=$elements}
})
[Console]::Out.Write((@{valid=$validSyntax; commands=$commands} | ConvertTo-Json -Depth 12 -Compress))
