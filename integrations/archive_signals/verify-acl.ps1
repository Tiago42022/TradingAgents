param([Parameter(Mandatory=$true)][string]$LiteralPath)
# Read-only verifier for the independent signal-device credential.
$ErrorActionPreference='Stop'
$item=Get-Item -LiteralPath $LiteralPath -Force
if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Reparse path.'}
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=Get-Acl -LiteralPath $LiteralPath
if(-not $acl.AreAccessRulesProtected){throw 'Unprotected ACL.'}
if($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value){throw 'Unexpected owner.'}
$rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
if($rules.Count -ne 1 -or $rules[0].IdentityReference.Value -ne $sid.Value -or $rules[0].AccessControlType -ne 'Allow' -or $rules[0].FileSystemRights -ne 'FullControl'){throw 'Unexpected access.'}
