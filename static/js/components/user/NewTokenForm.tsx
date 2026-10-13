import { useEffect, useState } from "react";

import Alert from "@mui/material/Alert";
import Autocomplete from "@mui/material/Autocomplete";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import FormControlLabel from "@mui/material/FormControlLabel";

import dayjs from "dayjs";

import { Controller, useForm } from "react-hook-form";
import Button1 from "../Button";

import { useCreateTokenMutation } from "../../ducks/profile";

interface NewTokenFormProps {
  availableAcls?: string[] | undefined;
  availableGroups?: { id: number; name: string }[] | undefined;
  availableStreams?: { id: number; name: string }[] | undefined;
}

const NewTokenForm = ({
  availableAcls,
  availableGroups,
  availableStreams,
}: NewTokenFormProps) => {
  const [createToken] = useCreateTokenMutation();
  const [groupIds, setGroupIds] = useState<number[]>([]);
  const [streamIds, setStreamIds] = useState<number[]>([]);
  const [newSecret, setNewSecret] = useState<string | null>(null);

  const {
    handleSubmit,
    register,
    reset,
    control,
    formState: { errors },
  } = useForm();

  useEffect(() => {
    reset({
      acls: Array(availableAcls?.length ?? 0).fill(false),
      overlap_seconds: "60",
      expires_at: dayjs().add(180, "day").format("YYYY-MM-DDTHH:mm"),
    });
  }, [reset, availableAcls]);

  const onSubmit = async (data: any) => {
    try {
      const result = (await createToken({
        name: data.name,
        acls: availableAcls?.filter((_acl, idx) => data.acls[idx]) ?? [],
        group_ids: groupIds,
        stream_ids: streamIds,
        expires_at: new Date(data.expires_at).toISOString(),
        overlap_seconds: Number(data.overlap_seconds || 60),
      }).unwrap()) as { token_id: string };
      setNewSecret(result.token_id);
      reset();
      setGroupIds([]);
      setStreamIds([]);
    } catch {
      // error notification handled by the API layer
    }
  };

  return (
    <Box
      component="form"
      onSubmit={handleSubmit(onSubmit)}
      sx={{ display: "flex", flexDirection: "column", gap: 2 }}
    >
      <Alert severity="info" sx={{ py: 0 }}>
        Scoped tokens carry a fixed snapshot of ACLs/groups/streams and an
        expiry; gaining permissions later never expands them. The secret is
        shown only once.
      </Alert>
      <Box
        sx={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "flex-start",
          gap: 1.5,
        }}
      >
        <TextField
          label="Token name"
          size="small"
          {...register("name", { required: true })}
          name="name"
          error={!!errors["name"]}
          helperText={errors["name"] ? "Required" : ""}
          sx={{ flex: "1 1 12rem", maxWidth: "20rem" }}
        />
        <TextField
          label="Expires at (UTC)"
          type="datetime-local"
          size="small"
          {...register("expires_at", { required: true })}
          name="expires_at"
          error={!!errors["expires_at"]}
          helperText={errors["expires_at"] ? "Required" : ""}
        />
        <TextField
          label="Rotation overlap (s)"
          type="number"
          size="small"
          slotProps={{ htmlInput: { min: 0, max: 3600 } }}
          {...register("overlap_seconds", {
            required: true,
            min: 0,
            max: 3600,
          })}
          name="overlap_seconds"
          sx={{ width: "10rem" }}
        />
        <Button1 primary type="submit" sx={{ height: 40 }}>
          Generate Token
        </Button1>
      </Box>
      <Box
        sx={{
          display: "flex",
          flexWrap: "wrap",
          gap: 2,
        }}
      >
        <Autocomplete
          multiple
          size="small"
          disableCloseOnSelect
          options={availableGroups ?? []}
          getOptionLabel={(option) => option.name}
          isOptionEqualToValue={(a, b) => a.id === b.id}
          value={(availableGroups ?? []).filter((g) =>
            groupIds.includes(g.id),
          )}
          onChange={(_event, value) => setGroupIds(value.map((g) => g.id))}
          renderOption={(props, option, { selected }) => (
            <li {...props} key={option.id}>
              <Checkbox size="small" checked={selected} />
              {option.name}
            </li>
          )}
          renderInput={(params) => (
            <TextField
              {...params}
              label="Groups (required)"
              data-testid="token-groups"
              sx={{ minWidth: "16rem" }}
            />
          )}
        />
        <Autocomplete
          multiple
          size="small"
          disableCloseOnSelect
          options={availableStreams ?? []}
          getOptionLabel={(option) => option.name}
          isOptionEqualToValue={(a, b) => a.id === b.id}
          value={(availableStreams ?? []).filter((s) =>
            streamIds.includes(s.id),
          )}
          onChange={(_event, value) => setStreamIds(value.map((s) => s.id))}
          renderOption={(props, option, { selected }) => (
            <li {...props} key={option.id}>
              <Checkbox size="small" checked={selected} />
              {option.name}
            </li>
          )}
          renderInput={(params) => (
            <TextField
              {...params}
              label="Streams (optional)"
              data-testid="token-streams"
              sx={{ minWidth: "16rem" }}
            />
          )}
        />
      </Box>
      <Box>
        <Typography variant="caption" color="textSecondary">
          Permissions
        </Typography>
        <Box
          sx={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(12rem, 1fr))",
            columnGap: 1,
          }}
        >
          {availableAcls?.map((acl, idx) => (
            <Controller
              key={acl}
              name={`acls[${idx}]`}
              control={control}
              defaultValue={false}
              render={({ field }) => (
                <FormControlLabel
                  label={<Typography variant="body2">{acl}</Typography>}
                  control={
                    <Checkbox
                      size="small"
                      checked={!!field.value}
                      onChange={field.onChange}
                      data-testid={`acls[${idx}]`}
                    />
                  }
                />
              )}
            />
          ))}
        </Box>
      </Box>

      <Dialog open={newSecret !== null} onClose={() => setNewSecret(null)}>
        <DialogTitle>Token secret — copy it now</DialogTitle>
        <DialogContent>
          <Typography variant="body2" gutterBottom>
            This secret is shown only once and cannot be recovered later.
            Rotate the token if you lose it.
          </Typography>
          <TextField
            fullWidth
            multiline
            slotProps={{ htmlInput: { readOnly: true } }}
            value={newSecret ?? ""}
            data-testid="new-token-secret"
            onFocus={(e) => e.target.select()}
          />
        </DialogContent>
        <DialogActions>
          <Button
            size="small"
            onClick={() => navigator.clipboard?.writeText(newSecret ?? "")}
          >
            Copy
          </Button>
          <Button onClick={() => setNewSecret(null)}>Done</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

export default NewTokenForm;
