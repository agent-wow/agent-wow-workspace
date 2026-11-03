// Command gendbc regenerates the client-data tables in the dbc package.
//
// It reads the pinned 3.3.5a DBC files fetched by scripts/fetch-client-data.sh
// and writes the generated Go sources next to this package. Every table is
// checked against the sha256 of the file it was produced from, and the skill
// line table is cross-checked against the SQL dump of the same table that
// wowgaming publishes, so a corrupted or substituted mirror cannot slip into
// the generated code.
//
//	go run ./dbc/cmd/gendbc -data-dir ../../data/client-data
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"flag"
	"fmt"
	"go/format"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"

	"github.com/doumoman/agent-wow-mods/wow/dbcfile"
)

// dbcSources pins the mirror files this generator accepts. The sha256 values
// match scripts/fetch-client-data.sh.
var dbcSources = []struct {
	name   string
	sha256 string
}{
	{"SkillLine.dbc", "276824940b7a38e1639e974571e5cb1ad0a1ecbaf3d7d14ebf02ab0294edcadb"},
	{"SkillTiers.dbc", "7bf8c5f87fc7917fa3bb42b459246fd8c5870e5223d24d66ae16e27e730c7e49"},
	{"SkillRaceClassInfo.dbc", "341a42678faee5f208e0874ab154ee174b99ba469306df7458bdaf0ffcf76967"},
}

// dbcBaseURL is where the mirror files live.
const dbcBaseURL = "https://api.github.com/repos/wowgaming/node-dbc-reader/contents/data/dbc"

// localeSlots is the order of the sixteen locale slots inside a 3.3.5a DBC.
// The slot order is not the same as the column order of the SQL dump, which
// is why the tables are generated from the raw DBC.
var localeSlots = []string{
	"enUS", "koKR", "frFR", "deDE", "enCN", "zhCN", "enTW", "zhTW",
	"enGB", "esES", "esMX", "ruRU", "ptPT", "ptBR", "itIT", "unk",
}

// localePreference is the order in which a display name is picked when a
// table carries more than one locale.
var localePreference = []string{"zhCN", "zhTW", "enUS", "enGB"}

// stringBlock is a run of sixteen locale fields inside a DBC record.
type stringBlock struct {
	first int // field index of the first locale slot
}

func main() {
	dataDir := flag.String("data-dir", "", "directory holding the fetched client data (contains dbc/ and AzerothcoreDBCToSQL/)")
	outDir := flag.String("out", defaultOutDir(), "directory to write the generated Go sources into")
	flag.Parse()

	if *dataDir == "" {
		log.Fatal("gendbc: -data-dir is required")
	}
	if err := run(*dataDir, *outDir); err != nil {
		log.Fatalf("gendbc: %v", err)
	}
}

// defaultOutDir is the dbc package directory, resolved from this source file
// so the command writes to the right place no matter where it is run from.
func defaultOutDir() string {
	_, self, _, ok := runtime.Caller(0)
	if !ok {
		return "."
	}
	// <pkg>/dbc/cmd/gendbc/main.go -> <pkg>/dbc
	return filepath.Clean(filepath.Join(filepath.Dir(self), "..", ".."))
}

func run(dataDir, outDir string) error {
	dbcDir := filepath.Join(dataDir, "dbc")
	skillLine, err := loadTable(dbcDir, "SkillLine.dbc")
	if err != nil {
		return err
	}
	tiers, err := loadTable(dbcDir, "SkillTiers.dbc")
	if err != nil {
		return err
	}
	raceClass, err := loadTable(dbcDir, "SkillRaceClassInfo.dbc")
	if err != nil {
		return err
	}

	// The SQL dump of the same table is the upstream cross-check. It is
	// optional: the SQL release is only fetched for this purpose.
	sqlPath := filepath.Join(dataDir, "AzerothcoreDBCToSQL", "skillline_dbc.sql")
	if _, err := os.Stat(sqlPath); err == nil {
		if err := crossCheckSkillLine(skillLine, sqlPath); err != nil {
			return fmt.Errorf("cross-check against %s: %w", sqlPath, err)
		}
		fmt.Println("skill line table matches the upstream SQL dump")
	} else {
		fmt.Println("warning: skillline_dbc.sql not found, skipping the upstream cross-check")
	}

	if err := writeSkillLines(skillLine, outDir); err != nil {
		return err
	}
	if err := writeSkillTiers(tiers, outDir); err != nil {
		return err
	}
	if err := writeSkillRaceClassInfo(raceClass, outDir); err != nil {
		return err
	}
	return nil
}

// loadTable reads a DBC file and checks it against the pinned checksum.
func loadTable(dir, name string) (*dbcfile.File, error) {
	path := filepath.Join(dir, name)
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	want := ""
	for _, src := range dbcSources {
		if src.name == name {
			want = src.sha256
		}
	}
	if want == "" {
		return nil, fmt.Errorf("%s is not a pinned source", name)
	}
	sum := sha256.Sum256(data)
	got := hex.EncodeToString(sum[:])
	if got != want {
		return nil, fmt.Errorf("%s: sha256 %s does not match the pinned %s", path, got, want)
	}
	file, err := dbcfile.ParseFile(data)
	if err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	return file, nil
}

// pickLocale returns the best display name for a sixteen slot string block.
func pickLocale(file *dbcfile.File, record, first int) string {
	slot := func(i int) string { return file.String(record, first+i) }
	for _, want := range localePreference {
		for i, name := range localeSlots {
			if name != want {
				continue
			}
			if v := slot(i); v != "" {
				return v
			}
		}
	}
	for i := range localeSlots {
		if v := slot(i); v != "" {
			return v
		}
	}
	return ""
}

func writeSkillLines(file *dbcfile.File, outDir string) error {
	var b strings.Builder
	writeHeader(&b, "SkillLine.dbc", "skillLines")
	b.WriteString("var skillLines = map[uint16]SkillLine{\n")
	for i := 0; i < file.Len(); i++ {
		id := file.Uint32(i, 0)
		if id == 0 || id > 0xFFFF {
			continue
		}
		name := pickLocale(file, i, 3)
		category := int32(file.Uint32(i, 1))
		icon := file.Uint32(i, 37)
		fmt.Fprintf(&b, "\t%d: {ID: %d, Name: %q, Category: %d, IconID: %d},\n",
			id, id, name, category, icon)
	}
	b.WriteString("}\n")
	return writeFile(outDir, "skillline_gen.go", b.String())
}

func writeSkillTiers(file *dbcfile.File, outDir string) error {
	var b strings.Builder
	writeHeader(&b, "SkillTiers.dbc", "skillTiers")
	b.WriteString("var skillTiers = map[uint16]SkillTier{\n")
	for i := 0; i < file.Len(); i++ {
		id := file.Uint32(i, 0)
		if id == 0 || id > 0xFFFF {
			continue
		}
		fmt.Fprintf(&b, "\t%d: {ID: %d, Cost: [16]uint32{", id, id)
		for j := 0; j < 16; j++ {
			if j > 0 {
				b.WriteString(", ")
			}
			fmt.Fprintf(&b, "%d", file.Uint32(i, 1+j))
		}
		b.WriteString("}, Value: [16]uint32{")
		for j := 0; j < 16; j++ {
			if j > 0 {
				b.WriteString(", ")
			}
			fmt.Fprintf(&b, "%d", file.Uint32(i, 17+j))
		}
		b.WriteString("}},\n")
	}
	b.WriteString("}\n")
	return writeFile(outDir, "skilltiers_gen.go", b.String())
}

func writeSkillRaceClassInfo(file *dbcfile.File, outDir string) error {
	var b strings.Builder
	writeHeader(&b, "SkillRaceClassInfo.dbc", "skillRaceClassInfo")
	b.WriteString("var skillRaceClassInfo = map[uint16]SkillRaceClassInfo{\n")
	for i := 0; i < file.Len(); i++ {
		id := file.Uint32(i, 0)
		if id == 0 || id > 0xFFFF {
			continue
		}
		fmt.Fprintf(&b, "\t%d: {ID: %d, SkillID: %d, RaceMask: %d, ClassMask: %d, Flags: %d, SkillTierID: %d},\n",
			id, id,
			file.Uint32(i, 1), file.Uint32(i, 2), file.Uint32(i, 3),
			file.Uint32(i, 4), file.Uint32(i, 6))
	}
	b.WriteString("}\n")
	return writeFile(outDir, "skillraceclassinfo_gen.go", b.String())
}

func writeHeader(b *strings.Builder, file, variable string) {
	sum := ""
	for _, src := range dbcSources {
		if src.name == file {
			sum = src.sha256
		}
	}
	fmt.Fprintf(b, "// Code generated by lib/wow/dbc/cmd/gendbc. DO NOT EDIT.\n")
	fmt.Fprintf(b, "//\n")
	fmt.Fprintf(b, "// Source: %s/%s\n", dbcBaseURL, file)
	fmt.Fprintf(b, "//         (mirror of wowgaming/client-data v20.0, build 12340)\n")
	fmt.Fprintf(b, "// sha256: %s\n", sum)
	fmt.Fprintf(b, "//\n")
	fmt.Fprintf(b, "// Variable: %s\n", variable)
	fmt.Fprintf(b, "\n")
	fmt.Fprintf(b, "package dbc\n\n")
}

func writeFile(dir, name, body string) error {
	path := filepath.Join(dir, name)
	formatted, err := format.Source([]byte(body))
	if err != nil {
		return fmt.Errorf("format %s: %w", name, err)
	}
	if err := os.WriteFile(path, formatted, 0o644); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	fmt.Printf("wrote %s\n", path)
	return nil
}

// crossCheckSkillLine compares the generated skill line table against the SQL
// dump of the same table published by wowgaming.
//
// The dump uses the wide modern schema: every string block is 32 locale
// columns wide, of which the first sixteen carry the 3.3.5a slots. The
// mapping below turns a dump column into the DBC field it holds.
func crossCheckSkillLine(file *dbcfile.File, sqlPath string) error {
	data, err := os.ReadFile(sqlPath)
	if err != nil {
		return err
	}
	byID := make(map[uint32]int, file.Len())
	for i := 0; i < file.Len(); i++ {
		byID[file.Uint32(i, 0)] = i
	}
	rows := 0
	for _, values := range parseSQLRows(string(data)) {
		if len(values) < 56 {
			return fmt.Errorf("unexpected column count %d", len(values))
		}
		id, err := strconv.ParseUint(values[0], 10, 32)
		if err != nil {
			return fmt.Errorf("bad id %q: %w", values[0], err)
		}
		record, ok := byID[uint32(id)]
		if !ok {
			return fmt.Errorf("skill line %d is missing from the DBC", id)
		}
		for col, value := range values {
			field := sqlColumnToField(col)
			if field < 0 {
				if value != "" {
					return fmt.Errorf("skill line %d: column %d holds %q but has no DBC field", id, col, value)
				}
				continue
			}
			if isStringField(field) {
				got := file.String(record, field)
				if got != value {
					return fmt.Errorf("skill line %d field %d: dump has %q, DBC has %q", id, field, value, got)
				}
				continue
			}
			want, err := strconv.ParseInt(value, 10, 64)
			if err != nil {
				return fmt.Errorf("skill line %d field %d: bad number %q", id, field, value)
			}
			got := int32(file.Uint32(record, field))
			if uint32(got) != uint32(want) {
				return fmt.Errorf("skill line %d field %d: dump has %d, DBC has %d", id, field, want, got)
			}
		}
		rows++
	}
	if rows != file.Len() {
		return fmt.Errorf("dump has %d rows, DBC has %d", rows, file.Len())
	}
	return nil
}

// sqlColumnToField maps a column of the wide SQL dump to the DBC field it
// holds, or -1 when the column has no counterpart.
func sqlColumnToField(col int) int {
	switch {
	case col <= 18:
		return col
	case col <= 34:
		return -1
	case col == 35:
		return 19
	case col <= 51:
		return col - 16
	case col <= 67:
		return -1
	case col == 68:
		return 36
	case col == 69:
		return 37
	case col <= 85:
		return col - 32
	case col <= 101:
		return -1
	case col == 102:
		return 54
	default:
		return 55
	}
}

// isStringField reports whether a DBC field holds a string offset.
func isStringField(field int) bool {
	switch {
	case field >= 3 && field <= 18:
		return true
	case field >= 20 && field <= 35:
		return true
	case field >= 38 && field <= 53:
		return true
	}
	return false
}

// parseSQLRows returns the VALUES tuples of every skill line row in the dump.
func parseSQLRows(data string) [][]string {
	const marker = "INSERT INTO `skillline_dbc` VALUES ("
	var rows [][]string
	for {
		start := strings.Index(data, marker)
		if start < 0 {
			return rows
		}
		rest := data[start+len(marker):]
		values, consumed := parseSQLTuple(rest)
		rows = append(rows, values)
		data = rest[consumed:]
	}
}

// parseSQLTuple splits one VALUES tuple into its columns and reports how
// many bytes it consumed.
func parseSQLTuple(s string) ([]string, int) {
	var (
		out     []string
		cur     strings.Builder
		inQuote bool
	)
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case inQuote && c == '\\':
			if i+1 < len(s) {
				cur.WriteByte(s[i+1])
				i++
			}
		case c == '"':
			inQuote = !inQuote
			if !inQuote {
				out = append(out, cur.String())
				cur.Reset()
			}
		case !inQuote && c == ',':
			out = append(out, strings.TrimSpace(cur.String()))
			cur.Reset()
		case !inQuote && c == ')':
			out = append(out, strings.TrimSpace(cur.String()))
			return out, i + 1
		default:
			cur.WriteByte(c)
		}
	}
	return out, len(s)
}
