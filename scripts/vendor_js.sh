#!/bin/sh
# Copia as bibliotecas JavaScript da página do modo assist para dentro do
# projeto (sem CDN em tempo de execução): marked, DOMPurify e mermaid, em
# versões fixas, a partir do registro do npm, com as licenças.
#
# Uso: scripts/vendor_js.sh   (precisa de curl, tar e sha256sum; só rede na hora)
set -eu
cd "$(dirname "$0")/.."
DEST=src/wendes/assistant/web/static/vendor
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$DEST"

# pacote versão arquivo-no-tarball destino sha256-do-tarball
fetch() {
    pkg=$1 ver=$2 file=$3 out=$4 sum=$5
    url="https://registry.npmjs.org/$pkg/-/$pkg-$ver.tgz"
    echo "↓ $pkg@$ver"
    curl -fsSL -o "$TMP/$pkg.tgz" "$url"
    if [ -n "$sum" ]; then
        echo "$sum  $TMP/$pkg.tgz" | sha256sum -c - >/dev/null
    fi
    mkdir -p "$TMP/$pkg"
    tar -xzf "$TMP/$pkg.tgz" -C "$TMP/$pkg"
    cp "$TMP/$pkg/package/$file" "$DEST/$out"
    lic=$(ls "$TMP/$pkg/package" | grep -i '^licen[cs]e' | head -1 || true)
    if [ -n "$lic" ]; then
        cp "$TMP/$pkg/package/$lic" "$DEST/LICENSE.$pkg.txt"
    fi
    echo "$pkg $ver $out" >> "$TMP/VERSIONS"
}

fetch marked 12.0.2 marked.min.js marked.min.js "${MARKED_SHA256:-6cfd2d09c6bce2541558a1547e5f3b9895ed743f0d287536ff2280e318e8a074}"
fetch dompurify 3.1.6 dist/purify.min.js purify.min.js "${DOMPURIFY_SHA256:-385981ebd0366fcce5313c1801019af737260ca29fe60d38a97f956d5ff1013a}"
fetch mermaid 10.9.1 dist/mermaid.min.js mermaid.min.js "${MERMAID_SHA256:-370ad9d7815e5fb54a059efce9be436740b74849b6fcdff21d1565379c6cd073}"
cp "$TMP/VERSIONS" "$DEST/VERSIONS.txt"
echo "Pronto: $DEST"
ls -la "$DEST"
