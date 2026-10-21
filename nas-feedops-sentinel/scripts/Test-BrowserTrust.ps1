# Test-BrowserTrust.ps1
# Says, in plain words, why a browser on this PC would warn ("Not secure")
# about the stack's local HTTPS addresses, and who must act.
#
#   .\nas-feedops-sentinel\scripts\Test-BrowserTrust.ps1              # 8443 and 8081
#   .\nas-feedops-sentinel\scripts\Test-BrowserTrust.ps1 -Port 8443
#
# What it checks:
#   the root   the offline root's public certificate (-RootFile, by default
#              instraclustr\certs\trust\root-ca.crt): its subject, its
#              thumbprint, and whether Windows trusts it (that thumbprint is in
#              Cert:\CurrentUser\Root or Cert:\LocalMachine\Root).
#   each port  -Port, by default 8443 (the dashboard, behind the edge) and 8081
#              (Keycloak's sign-in page). It connects over TLS to localhost, the
#              name the browser uses, and prints who issued the certificate the
#              server presents, its end date, the names it lists and Windows'
#              own verdict on its chain. Then one verdict: what is wrong, who
#              acts, and the next step.
#
# Windows' verdict stands in for Chrome's. Chrome uses its own verifier, on the
# same trusted roots in Windows' certificate store. If this says all clear and
# Chrome still warns, the verdict says what to pass on.
#
# It changes nothing on this PC. It reads only -RootFile and what the server
# presents in the TLS handshake, and it stops after the handshake: it sends no
# request and no credential. It never imports, removes or edits a certificate
# or a trust setting, and starts or stops nothing. When Windows does not trust
# the root, it prints the command for the person at this PC to run.
#
# The steps for every verdict: nas-feedops-sentinel\docs\runbooks.md, "The
# browser says Not secure". Exit 0: every port is all clear. 1: a port has a
# problem. 2: the check could not run (the root file is missing or unreadable).
[CmdletBinding()]
param(
    # Default: instraclustr\certs\trust\root-ca.crt in this repository (set
    # below: Windows PowerShell leaves $PSScriptRoot empty in a default here).
    [string]$RootFile,
    [ValidateRange(1, 65535)]
    [int[]]$Port = @(8443, 8081)
)

$ErrorActionPreference = 'Stop'
trap { Write-Host "The check could not run: $($_.Exception.Message)" -ForegroundColor Red; exit 2 }
if (-not $RootFile) {
    $RootFile = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'instraclustr\certs\trust\root-ca.crt'
}
$runbook = 'nas-feedops-sentinel\docs\runbooks.md, "The browser says Not secure"'

function Format-Utc([datetime]$Time) { return $Time.ToUniversalTime().ToString('yyyy-MM-dd HH:mm') + ' UTC' }

# One verdict per port: what is wrong, who acts, the next step, and where the steps are.
function Write-Verdict([bool]$Clear, [string]$What, [string]$Who, [string[]]$Next) {
    $colour = if ($Clear) { 'Green' } else { 'Red' }
    Write-Host "  Verdict:     $What" -ForegroundColor $colour
    Write-Host "  Who acts:    $Who"
    Write-Host "  Next:        $($Next[0])"
    foreach ($line in ($Next | Select-Object -Skip 1)) { Write-Host "               $line" }
    Write-Host "  Steps:       $runbook"
}

# -- The root -----------------------------------------------------------------
if (-not (Test-Path -LiteralPath $RootFile -PathType Leaf)) {
    Write-Host "The offline root's certificate is not at $RootFile, so nothing was checked." -ForegroundColor Red
    Write-Host 'Who acts: whoever runs the stack. Next: run the root ceremony (instraclustr\certs\README.md), then run this check again.'
    exit 2
}
$rootPath = (Resolve-Path -LiteralPath $RootFile).ProviderPath
try {
    $root = New-Object Security.Cryptography.X509Certificates.X509Certificate2 -ArgumentList $rootPath
} catch {
    Write-Host "$rootPath is not a certificate this check can read: $($_.Exception.GetBaseException().Message.Trim())" -ForegroundColor Red
    Write-Host 'Who acts: whoever runs the stack. Next: .\instraclustr\scripts\pki.ps1 check says what is wrong with the files from the root ceremony (instraclustr\certs\README.md).'
    exit 2
}
$rootName = $root.GetNameInfo('SimpleName', $false)
$trustedIn = @(foreach ($store in 'Cert:\CurrentUser\Root', 'Cert:\LocalMachine\Root') {
    if (Get-ChildItem -Path $store | Where-Object Thumbprint -eq $root.Thumbprint) { $store }
})

Write-Host "The offline root: $rootPath"
Write-Host "  Subject:     $($root.Subject)"
Write-Host "  Thumbprint:  $($root.Thumbprint)"
if ($trustedIn) {
    Write-Host "  Windows trusts it: yes, it is in $($trustedIn -join ' and ')"
} else {
    Write-Host '  Windows trusts it: no, it is in neither Cert:\CurrentUser\Root nor Cert:\LocalMachine\Root'
}

# What one TLS handshake shows: the certificate the server presents, the chain
# Windows builds for it, and Windows' verdict. In C#, because the validation
# callback can run on a thread where a PowerShell script block cannot. The
# callback accepts the certificate only so the handshake ends and the
# certificate can be read; nothing is sent after the handshake. (Windows
# PowerShell compiles this through temporary files, which it deletes.)
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Authentication;
using System.Security.Cryptography.X509Certificates;

public class BrowserTrustHandshake
{
    public X509Certificate2 Certificate;
    public List<X509Certificate2> Chain = new List<X509Certificate2>();
    public List<string> ChainStatus = new List<string>();
    public SslPolicyErrors Errors;
    public string Failure;

    public static BrowserTrustHandshake Run(string host, int port)
    {
        BrowserTrustHandshake seen = new BrowserTrustHandshake();
        // Every address the name has: localhost may resolve to ::1 first, and
        // the stack listens on 127.0.0.1 only.
        foreach (IPAddress address in Dns.GetHostAddresses(host))
        {
            try
            {
                using (TcpClient tcp = new TcpClient(address.AddressFamily))
                {
                    tcp.ReceiveTimeout = 10000;
                    tcp.SendTimeout = 10000;
                    tcp.Connect(address, port);
                    using (SslStream tls = new SslStream(tcp.GetStream(), false, seen.Record))
                    {
                        // TLS 1.2, which the edge and Keycloak accept; Windows
                        // PowerShell would otherwise offer only older versions.
                        tls.AuthenticateAsClient(host, null, SslProtocols.Tls12, false);
                    }
                }
                seen.Failure = null;
                return seen;
            }
            catch (Exception e)
            {
                seen.Failure = e.GetBaseException().Message;
                if (seen.Certificate != null) return seen;
            }
        }
        return seen;
    }

    private bool Record(object sender, X509Certificate certificate, X509Chain chain, SslPolicyErrors errors)
    {
        if (certificate == null) return false;
        Certificate = new X509Certificate2(certificate);
        Errors = errors;
        foreach (X509ChainElement element in chain.ChainElements) Chain.Add(new X509Certificate2(element.Certificate));
        foreach (X509ChainStatus status in chain.ChainStatus) ChainStatus.Add(status.Status.ToString());
        return true;
    }
}
'@

# -- Each port ----------------------------------------------------------------
$problems = 0
foreach ($p in $Port) {
    Write-Host ''
    Write-Host "https://localhost:$p"
    $seen = [BrowserTrustHandshake]::Run('localhost', $p)
    $leaf = $seen.Certificate
    if (-not $leaf) {
        $problems++
        Write-Verdict $false "nothing answered on port $p ($($seen.Failure)). This is not a certificate problem: the stack may be down." `
            'whoever runs the stack.' `
            @('check that the stack is up (docker ps shows nas-edge and nas-keycloak), start it if it is not, then run this check again.')
        continue
    }

    $san = $leaf.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.17' }
    $names = if ($san) { $san.Format($false) } else { 'none' }
    $chainErrors = @($seen.ChainStatus | Where-Object { $_ -ne 'NoError' })
    $nameMismatch = [bool]($seen.Errors -band [Net.Security.SslPolicyErrors]::RemoteCertificateNameMismatch)
    $reasons = @($chainErrors)
    if ($nameMismatch) { $reasons += 'the name localhost is not listed' }
    if (-not $reasons) { $reasons = @("$($seen.Errors)") }
    $windows = if ($seen.Errors -eq 'None') { 'trusts it for localhost' } else { "does not trust it: $($reasons -join ', ')" }

    Write-Host "  Issued by:   $($leaf.Issuer)"
    Write-Host "  Valid until: $(Format-Utc $leaf.NotAfter)"
    Write-Host "  Names:       $names"
    Write-Host "  Chain:       $(($seen.Chain | ForEach-Object { $_.GetNameInfo('SimpleName', $false) }) -join ' <- ')"
    Write-Host "  Windows:     $windows"

    # Does it come from the offline root at all? Windows builds the chain again
    # with the root offered (not trusted), so a certificate from any other
    # authority, such as the retired "NAS FeedOps Sentinel Local CA", ends
    # somewhere else.
    $link = New-Object Security.Cryptography.X509Certificates.X509Chain
    $link.ChainPolicy.RevocationMode = 'NoCheck'
    $link.ChainPolicy.VerificationFlags = 'AllowUnknownCertificateAuthority'
    [void]$link.ChainPolicy.ExtraStore.Add($root)
    foreach ($cert in $seen.Chain) { [void]$link.ChainPolicy.ExtraStore.Add($cert) }
    [void]$link.Build($leaf)
    $linked = @($link.ChainElements | ForEach-Object { $_.Certificate })
    $now = Get-Date
    $outOfDate = @($linked | Where-Object { $_.NotBefore -gt $now -or $_.NotAfter -lt $now })
    # When the chain ends at an authority Windows does not trust, Windows says
    # PartialChain (the server does not send the root, so the chain stops
    # below it) or UntrustedRoot.
    $untrustedOnly = $chainErrors.Count -gt 0 -and -not ($chainErrors | Where-Object { $_ -notin 'PartialChain', 'UntrustedRoot' })

    if ($seen.Errors -eq 'None' -and $linked[-1].Thumbprint -eq $root.Thumbprint) {
        Write-Verdict $true "all clear: Windows trusts the certificate on port $p for localhost." `
            'the person at this PC, only if Chrome still warns.' `
            @('close every Chrome window (Incognito too) and open the address again.',
              'If the warning stays, read the error code under the warning''s headline (for example NET::ERR_CERT_AUTHORITY_INVALID) and pass it on to whoever runs the stack.')
        continue
    }
    $problems++
    if ($linked[-1].Thumbprint -ne $root.Thumbprint) {
        Write-Verdict $false "the certificate does not come from the offline root in $rootPath; it was issued by $($leaf.Issuer). The stack is serving an old certificate." `
            'whoever runs the stack.' `
            @('run the edge-certs step and recreate the containers (the runbook has the commands), then run this check again.',
              'Do not trust another certificate authority to make the warning go away.')
    } elseif ($outOfDate) {
        $old = $outOfDate[0]
        $when = if ($old.NotAfter -lt $now) { "expired on $(Format-Utc $old.NotAfter)" } else { "is not valid until $(Format-Utc $old.NotBefore)" }
        Write-Verdict $false "a certificate in the chain, $($old.GetNameInfo('SimpleName', $false)), $when. Either it is out of date, or this PC's clock is wrong (it says $(Format-Utc $now))." `
            'the person at this PC first, then whoever runs the stack.' `
            @("the person at this PC checks this PC's date, time and time zone.",
              'If they are right, whoever runs the stack renews the certificate (the runbook says how), then runs this check again.')
    } elseif ($nameMismatch) {
        Write-Verdict $false "the certificate does not list localhost, the name the browser uses. It lists: $names." `
            'whoever runs the stack.' `
            @('run the edge-certs step and recreate the containers, so the port serves the certificate edge-certs issues for localhost, then run this check again.')
    } elseif ($untrustedOnly) {
        Write-Verdict $false 'the certificate comes from the offline root, but Windows does not trust that root.' `
            'only the person at this PC can fix this. No script or agent does it for you.' `
            @('in PowerShell, run this once (no administrator rights needed):',
              ('    Import-Certificate -FilePath "{0}" -CertStoreLocation Cert:\CurrentUser\Root' -f $rootPath),
              "Windows asks you to confirm a certificate authority named `"$rootName`" and shows its thumbprint (sha1), which must be $($root.Thumbprint).",
              'Then close every Chrome window (Incognito too) and run this check again.')
    } else {
        Write-Verdict $false "Windows does not accept the certificate: $($reasons -join ', ')." `
            'whoever runs the stack.' `
            @('run .\instraclustr\scripts\pki.ps1 check, which checks the certificates under the root, and keep the status name above.',
              'A fault in the certificates themselves is a human decision, not a quick fix.')
    }
}

Write-Host ''
if ($problems) {
    Write-Host "$problems of $($Port.Count) address(es) need attention (above)." -ForegroundColor Red
    exit 1
}
Write-Host 'Every address is all clear.' -ForegroundColor Green
exit 0
