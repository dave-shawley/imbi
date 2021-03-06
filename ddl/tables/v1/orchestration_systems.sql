SET search_path=v1;

CREATE TABLE IF NOT EXISTS orchestration_systems (
  "name"      TEXT NOT NULL PRIMARY KEY,
  created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_at TIMESTAMP WITH TIME ZONE,
  description TEXT,
  icon_class  TEXT NOT NULL DEFAULT 'fas fa-hand-point-right'
);

COMMENT ON TABLE orchestration_systems IS 'Systems used for project orchestration';
COMMENT ON COLUMN orchestration_systems.name IS 'Orchestration system name';
COMMENT ON COLUMN orchestration_systems.created_at IS 'When the record was created at';
COMMENT ON COLUMN orchestration_systems.modified_at IS 'When the record was last modified';
COMMENT ON COLUMN orchestration_systems.description IS 'Description of the orchestration system';
COMMENT ON COLUMN orchestration_systems.icon_class IS 'Font Awesome UI icon class';

GRANT SELECT ON orchestration_systems TO reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON orchestration_systems TO admin;
