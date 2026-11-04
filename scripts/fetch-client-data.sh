#!/usr/bin/env bash
# Fetch the pinned 3.3.5a client-data tables this workspace decodes against.
#
# Two upstream sources, both pinned by sha256:
#
#   * wowgaming/client-data, release dbc_sql_v1 - the SQL dump of every DBC.
#     This is the upstream agent-wow itself pins. We use skillline_dbc.sql to
#     cross-check the DBC tables we generate from.
#
#   * wowgaming/node-dbc-reader - a byte mirror of the same 246 DBC files, so
#     single tables can be pulled without downloading the 1.2 GB Data.zip.
#     The tables this workspace needs (SkillTiers, SkillRaceClassInfo) are not
#     part of the SQL release at all.
#
# Everything lands in data/client-data/, which is gitignored: the client data
# is Blizzard content and must not be committed.
#
#     scripts/fetch-client-data.sh
#     scripts/fetch-client-data.sh --terrain  # also fetch outdoor height maps
#
# See docs/data-sources.md for the full provenance map.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$ROOT/data/client-data"
SQL_DIR="$DATA/AzerothcoreDBCToSQL"
DBC_DIR="$DATA/dbc"

SQL_URL="https://github.com/wowgaming/client-data/releases/download/dbc_sql_v1/AzerothcoreDBCToSQL.zip"
SQL_SHA256="e524aba0bc05aa06e2b6e5421eb80a2358fec1ae27f3ff352c15b2b75081737e"

DBC_BASE="https://api.github.com/repos/wowgaming/node-dbc-reader/contents/data/dbc"

TERRAIN_DIR="$DATA/terrain-v20"
TERRAIN_URL="https://github.com/wowgaming/client-data/releases/download/v20.0/Data.zip"
TERRAIN_SHA256="a3d4df635ae6c2c8f08052c32a79e0f806955150ad36b014a823dd08a32a4610"

# Tables mirrored from the DBC archive. Keep the sha256 in sync with
# upstream: `shasum -a 256 <file>`.
DBC_FILES=(
	"SkillLine.dbc 276824940b7a38e1639e974571e5cb1ad0a1ecbaf3d7d14ebf02ab0294edcadb"
	"SkillTiers.dbc 7bf8c5f87fc7917fa3bb42b459246fd8c5870e5223d24d66ae16e27e730c7e49"
	"SkillRaceClassInfo.dbc 341a42678faee5f208e0874ab154ee174b99ba469306df7458bdaf0ffcf76967"
)

# Tables pulled out of the SQL release for cross-checking only.
SQL_TABLES=(skillline_dbc.sql)

sha256_of() {
	shasum -a 256 "$1" | cut -d' ' -f1
}

verify() {
	local file="$1" want="$2"
	local got
	got="$(sha256_of "$file")"
	if [ "$got" != "$want" ]; then
		echo "sha256 mismatch for $file" >&2
		echo "  expected $want" >&2
		echo "  got      $got" >&2
		exit 1
	fi
}

fetch_sql() {
	local zip="$DATA/AzerothcoreDBCToSQL.zip"
	mkdir -p "$DATA" "$SQL_DIR"
	if [ ! -f "$zip" ] || [ "$(sha256_of "$zip")" != "$SQL_SHA256" ]; then
		echo "downloading $SQL_URL"
		curl -sSL --retry 3 -o "$zip.part" "$SQL_URL"
		verify "$zip.part" "$SQL_SHA256"
		mv "$zip.part" "$zip"
	fi
	echo "using verified $zip"
	for table in "${SQL_TABLES[@]}"; do
		unzip -o -q "$zip" "AzerothcoreDBCToSQL/$table" -d "$DATA"
		echo "extracted $table"
	done
}

fetch_dbc() {
	mkdir -p "$DBC_DIR"
	for entry in "${DBC_FILES[@]}"; do
		local name sha
		name="${entry%% *}"
		sha="${entry##* }"
		local dest="$DBC_DIR/$name"
		if [ ! -f "$dest" ] || [ "$(sha256_of "$dest")" != "$sha" ]; then
			echo "downloading $name"
			curl -sSL --retry 3 -H "Accept: application/vnd.github.raw" \
				-o "$dest.part" "$DBC_BASE/$name"
			verify "$dest.part" "$sha"
			mv "$dest.part" "$dest"
		fi
		echo "using verified $dest"
	done
}

fetch_terrain() {
	local zip="$TERRAIN_DIR/Data.zip"
	mkdir -p "$TERRAIN_DIR"
	if [ ! -f "$zip" ] || [ "$(sha256_of "$zip")" != "$TERRAIN_SHA256" ]; then
		echo "downloading $TERRAIN_URL"
		curl -fSL --retry 3 -o "$zip.part" "$TERRAIN_URL"
		verify "$zip.part" "$TERRAIN_SHA256"
		mv "$zip.part" "$zip"
	fi
	echo "using verified $zip"
	if zipinfo -1 "$zip" | grep -E '(^/|(^|/)\.\.(/|$))'; then
		echo "unsafe path in terrain archive" >&2
		exit 1
	fi
	unzip -o -q "$zip" "maps/*" -d "$TERRAIN_DIR"
	echo "extracted outdoor terrain to $TERRAIN_DIR/maps"
}

case "${1:-}" in
	"")
		fetch_sql
		fetch_dbc
		;;
	--terrain)
		fetch_terrain
		;;
	*)
		echo "usage: $0 [--terrain]" >&2
		exit 2
		;;
esac

echo
echo "client data ready in $DATA"
echo "regenerate the tables with:"
echo "  cd lib/wow && go run ./dbc/cmd/gendbc -data-dir $DATA"
