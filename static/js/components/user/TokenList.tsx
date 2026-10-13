import { useMemo, useState } from "react";

import AutorenewIcon from "@mui/icons-material/Autorenew";
import BlockIcon from "@mui/icons-material/Block";
import DeleteIcon from "@mui/icons-material/Delete";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import dayjs from "dayjs";

import ExpandableCell from "../ExpandableCell";
import StyledDataGrid, { DataGridToolbar } from "../StyledDataGrid";
import UpdateTokenACLs from "./UpdateTokenACLs";
import {
  TokenEntry,
  useDeleteGenerationMutation,
  useDeleteLegacyTokenMutation,
  useRevokeGenerationMutation,
  useRevokeTokenSeriesMutation,
  useRotateTokenSeriesMutation,
} from "../../ducks/profile";

const STATUS_COLORS: Record<string, any> = {
  active: "success",
  overlapping: "warning",
  expired: "default",
  revoked: "error",
  superseded: "default",
  owner_disabled: "error",
  source_removed: "error",
};

function ScopeChips({ items }: { items: { id: number; name: string }[] }) {
  if (!items?.length) return <Box color="text.disabled">—</Box>;
  return (
    <Stack direction="row" spacing={0.5} sx={{ flexWrap: "wrap", gap: 0.5 }}>
      {items.map((item) => (
        <Chip key={item.id} label={item.name} size="small" />
      ))}
    </Stack>
  );
}

function SecretDialog({
  secret,
  onClose,
}: {
  secret: string | null;
  onClose: () => void;
}) {
  return (
    <Dialog open={secret !== null} onClose={onClose} fullWidth>
      <DialogTitle>New token secret — copy it now</DialogTitle>
      <DialogContent>
        <TextField
          fullWidth
          multiline
          slotProps={{ htmlInput: { readOnly: true } }}
          value={secret ?? ""}
          data-testid="rotated-token-secret"
          onFocus={(e) => e.target.select()}
        />
      </DialogContent>
      <DialogActions>
        <Button
          size="small"
          onClick={() => navigator.clipboard?.writeText(secret ?? "")}
        >
          Copy
        </Button>
        <Button onClick={onClose}>Done</Button>
      </DialogActions>
    </Dialog>
  );
}

const TokenList = ({ tokens }: { tokens?: TokenEntry[] }) => {
  const [deleteGeneration] = useDeleteGenerationMutation();
  const [deleteLegacyToken] = useDeleteLegacyTokenMutation();
  const [revokeGeneration] = useRevokeGenerationMutation();
  const [revokeTokenSeries] = useRevokeTokenSeriesMutation();
  const [rotateTokenSeries] = useRotateTokenSeriesMutation();
  const [overlap, setOverlap] = useState("60");
  const [rotateTarget, setRotateTarget] = useState<TokenEntry | null>(null);
  const [newSecret, setNewSecret] = useState<string | null>(null);

  // only the newest generation of each series offers rotation
  const latestGenerationBySeries = useMemo(() => {
    const latest = new Map<string, number>();
    (tokens ?? []).forEach((t) => {
      if (!t.series_id) return;
      const gen = latest.get(t.series_id) ?? 0;
      if (t.generation > gen) latest.set(t.series_id, t.generation);
    });
    return latest;
  }, [tokens]);

  const doRotate = async () => {
    if (!rotateTarget?.series_id) return;
    try {
      const result = await rotateTokenSeries({
        seriesID: rotateTarget.series_id,
        overlap_seconds: Math.max(0, Math.min(3600, Number(overlap) || 60)),
      }).unwrap();
      setNewSecret(result.token_id);
    } finally {
      setRotateTarget(null);
    }
  };

  const columns: any[] = [
    {
      field: "name",
      headerName: "Name",
      width: 160,
      renderCell: ({ row }: any) => (
        <Box>
          <Box>{row.name}</Box>
          {row.series_id && (
            <Typography variant="caption" color="text.secondary">
              {row.series_id.slice(0, 8)} · gen {row.generation}
            </Typography>
          )}
        </Box>
      ),
    },
    {
      field: "fingerprint",
      headerName: "Fingerprint",
      width: 110,
      renderCell: ({ value }: any) => (
        <Tooltip title="First 8 characters of the secret; full value shown only at creation/rotation">
          <Box sx={{ fontFamily: "monospace" }}>{value}</Box>
        </Tooltip>
      ),
    },
    {
      field: "acls",
      headerName: "Permissions",
      width: 200,
      renderCell: ({ row }: any) => <ExpandableCell items={row.acls || []} />,
    },
    {
      field: "groups",
      headerName: "Groups",
      width: 200,
      renderCell: ({ row }: any) => <ScopeChips items={row.groups} />,
    },
    {
      field: "streams",
      headerName: "Streams",
      width: 200,
      renderCell: ({ row }: any) => <ScopeChips items={row.streams} />,
    },
    {
      field: "timing",
      headerName: "Expires / Retires",
      width: 240,
      sortable: false,
      renderCell: ({ row }: any) => (
        <Box>
          {row.expires_at ? (
            <Box>expires {dayjs(row.expires_at).format("YYYY/MM/DD HH:mm")}</Box>
          ) : (
            <Box color="text.disabled">no expiry (legacy)</Box>
          )}
          {row.retire_at && (
            <Box color="text.secondary">
              overlap ends {dayjs(row.retire_at).format("YYYY/MM/DD HH:mm")}
            </Box>
          )}
        </Box>
      ),
    },
    {
      field: "status",
      headerName: "Status",
      width: 130,
      renderCell: ({ value }: any) => (
        <Chip
          label={value}
          size="small"
          color={STATUS_COLORS[value] ?? "default"}
          variant={value === "active" ? undefined : "outlined"}
        />
      ),
    },
    {
      field: "delete",
      headerName: "Actions",
      width: 200,
      sortable: false,
      renderCell: ({ row }: any) => {
        const isLatest =
          !row.series_id ||
          latestGenerationBySeries.get(row.series_id) === row.generation;
        const rotatable =
          row.series_id && isLatest && row.status === "active";
        return (
          <Stack direction="row" spacing={0.5}>
            {!row.series_id && (
              <UpdateTokenACLs
                fingerprint={row.fingerprint}
                currentACLs={row.acls}
              />
            )}
            {rotatable && (
              <Tooltip title="Rotate (new generation in this series)">
                <IconButton
                  size="small"
                  data-testid={`rotate-${row.fingerprint}`}
                  onClick={() => {
                    setOverlap("60");
                    setRotateTarget(row);
                  }}
                >
                  <AutorenewIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            )}
            {row.series_id && row.status !== "revoked" && (
              <Tooltip title="Revoke this generation">
                <IconButton
                  size="small"
                  data-testid={`revoke-${row.fingerprint}`}
                  onClick={() =>
                    revokeGeneration({
                      seriesID: row.series_id,
                      generation: row.generation,
                    })
                  }
                >
                  <BlockIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            )}
            {row.series_id && row.status === "revoked" && (
              <Tooltip title="Revoke the whole series">
                <IconButton
                  size="small"
                  data-testid={`revoke-series-${row.fingerprint}`}
                  onClick={() => revokeTokenSeries(row.series_id)}
                >
                  <BlockIcon fontSize="small" color="error" />
                </IconButton>
              </Tooltip>
            )}
            <IconButton
              aria-label="Delete token"
              size="small"
              onClick={() =>
                row.series_id
                  ? deleteGeneration({
                      seriesID: row.series_id,
                      generation: row.generation,
                    })
                  : deleteLegacyToken(row.fingerprint)
              }
            >
              <DeleteIcon fontSize="small" />
            </IconButton>
          </Stack>
        );
      },
    },
  ];

  return (
    <>
      <StyledDataGrid
        autoHeight
        rows={tokens ?? []}
        columns={columns}
        getRowId={(row: any) =>
          row.series_id ? `${row.series_id}-${row.generation}` : row.fingerprint
        }
        components={{ Toolbar: DataGridToolbar }}
        componentsProps={{
          toolbar: { showExpandAll: true, title: "My Tokens" },
        }}
        sx={{ "& .MuiDataGrid-cell": { py: 1 } }}
        showToolbar
        getRowHeight={() => "auto"}
        initialState={{
          sorting: { sortModel: [{ field: "name", sort: "asc" }] },
        }}
      />

      <Dialog
        open={rotateTarget !== null}
        onClose={() => setRotateTarget(null)}
      >
        <DialogTitle>Rotate “{rotateTarget?.name}”</DialogTitle>
        <DialogContent>
          <TextField
            autoFocus
            margin="dense"
            label="Overlap window (seconds, 0–3600)"
            type="number"
            fullWidth
            value={overlap}
            onChange={(e) => setOverlap(e.target.value)}
            slotProps={{ htmlInput: { min: 0, max: 3600 } }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRotateTarget(null)}>Cancel</Button>
          <Button onClick={doRotate} data-testid="confirm-rotate">
            Rotate
          </Button>
        </DialogActions>
      </Dialog>

      <SecretDialog secret={newSecret} onClose={() => setNewSecret(null)} />
    </>
  );
};

export default TokenList;
