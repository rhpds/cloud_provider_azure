# HCP ARM template

`hcp.json` is the checked-in deployment artifact. Runtime requires Azure CLI and
ARM REST access, not Bicep or a preview CLI extension. `hcp.bicep` is its source,
adapted from Microsoft's published HCP example:
https://learn.microsoft.com/azure/openshift/howto-create-custom-hosted-cluster

Regenerate locally with `bicep build hcp.bicep` after source edits and inspect the
compiled JSON diff. The source defines all operator identities and scoped Azure
role assignments; the provider passes explicit network, version, sizing and
ownership parameters. Both cluster and node pool use API `2026-09-01-preview`.
Key Vault is public/RBAC-enabled, with seven-day soft deletion and no automatic
purging. No customer control-plane VM resources are present in this template.

Reference code copyright Microsoft Corporation, used under the MIT license below.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
