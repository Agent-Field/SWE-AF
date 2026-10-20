package furrow

import (
	"encoding/json"
	"os"
	"path/filepath"
)

func (m *Manager) registryPath() string { return filepath.Join(m.remotesRoot, "registry.json") }

func (m *Manager) loadRegistry() {
	data, err := os.ReadFile(m.registryPath())
	if err != nil {
		m.logf("furrow registry: starting empty: %v", err)
		return
	}
	var entries map[string]Entry
	if err := json.Unmarshal(data, &entries); err != nil || entries == nil {
		m.logf("furrow registry: ignoring corrupt file %q", m.registryPath())
		return
	}
	m.entries = entries
}

func (m *Manager) saveRegistryLocked() error {
	if err := os.MkdirAll(m.remotesRoot, 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(m.entries, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	tmp, err := os.CreateTemp(m.remotesRoot, ".registry-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, m.registryPath())
}
