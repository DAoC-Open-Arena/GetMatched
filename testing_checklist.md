# DAoC 3v3 Matchmaking Bot — Pre-Event Testing Checklist

> Run this checklist on your **staging server** before every event.
> You need **yourself + at least one other account** to test buttons.
> Mark each item ✅ / ❌ as you go.

---

## Setup

- [ ] Bot is running (`python -m daoc_bot`)
- [ ] `#matchmaking` channel exists and is visible to the bot
- [ ] `simulate.py` passes with no errors (`python simulate.py`)
- [ ] You have access to at least 2 Discord accounts (alt, friend, or browser session)

---

## BLOCK 1 — Registration

### 1.1 Happy path
- [ ] `/register_team team_name:Alpha` → ephemeral ✅ confirmation
- [ ] Panel for **Alpha** appears in `#matchmaking` with state `⏸️ Idle`
- [ ] Panel shows correct leader tag
- [ ] `/register_team team_name:Bravo` with second account → panel appears

### 1.2 Duplicate name
- [ ] `/register_team team_name:Alpha` again (same name) → ❌ error "already taken"

### 1.3 Already a leader
- [ ] Same account runs `/register_team team_name:Gamma` → ❌ error "already a leader"

### 1.4 Unregister
- [ ] `/unregister_team` with Alpha's account → panel disappears from channel
- [ ] `/unregister_team` again → ❌ error "no registered team"
- [ ] Re-register Alpha → works fine

---

## BLOCK 2 — Ready / Unready

### 2.1 Happy path
- [ ] Alpha clicks **✅ Ready** → panel updates to `🟢 In queue`
- [ ] Alpha clicks **⏸️ Unready** → panel reverts to `⏸️ Idle`

### 2.2 Wrong user
- [ ] A different account (not the leader) clicks **Ready** on Alpha's panel → ❌ ephemeral error

### 2.3 Double ready
- [ ] Alpha clicks **Ready**, then **Ready** again → ❌ error "can't ready up right now"

---

## BLOCK 3 — Match Proposal

### 3.1 Match found
- [ ] Alpha clicks **Ready**, then Bravo clicks **Ready**
- [ ] Proposal embed appears tagging both leaders
- [ ] Both team panels update to `🟡 Match proposed`
- [ ] Proposal shows `⏳ Waiting` for both teams

### 3.2 Partial accept
- [ ] Alpha clicks **✅ Accept Match**
- [ ] Proposal embed updates: Alpha shows `✅ Accepted`, Bravo still `⏳ Waiting`
- [ ] Match is **not** yet started

### 3.3 Wrong user on accept
- [ ] A third account (not part of the match) clicks Accept → ❌ "not part of this match"

### 3.4 Both accept
- [ ] Bravo clicks **✅ Accept Match**
- [ ] Proposal embed disappears
- [ ] **MATCH STARTED!** broadcast appears tagging both leaders
- [ ] Both panels update to `🔴 Match in progress`

---

## BLOCK 4 — Match End

### 4.1 End by leader 1
- [ ] Alpha clicks **🏁 Match Ended**
- [ ] Match panel disappears
- [ ] Announcement embed appears tagging both leaders
- [ ] Both panels revert to `⏸️ Idle`

### 4.2 End by leader 2
- [ ] Repeat 3.1 → 3.4, this time Bravo clicks **🏁 Match Ended** → same result

### 4.3 Wrong user on end
- [ ] Third account clicks **Match Ended** → ❌ "not part of this match"

### 4.4 Double end
- [ ] While a match is active, quickly click **Match Ended** twice (or two leaders click at same time)
- [ ] Only one announcement appears, no crash, both teams back to IDLE

---

## BLOCK 5 — Anti Instant-Rematch

### 5.1 Guard active with 3+ teams
- [ ] Alpha and Bravo just finished a match
- [ ] Alpha clicks Ready, Bravo clicks Ready, Charlie (third account) clicks Ready
- [ ] Alpha or Bravo is matched with **Charlie**, not with each other
- [ ] Footer of both panels shows "Last opponent: …"

### 5.2 Auto-lift with only 2 teams
- [ ] Alpha and Bravo just finished a match (guard active)
- [ ] **Only** Alpha and Bravo click Ready (no third team)
- [ ] After a moment, match is proposed between Alpha and Bravo anyway
- [ ] Guard was lifted automatically

---

## BLOCK 6 — Decline

### 6.1 Decline before other accepts
- [ ] Alpha and Bravo get a match proposal
- [ ] Alpha clicks **❌ Decline**
- [ ] Proposal disappears
- [ ] Cancellation message appears in channel (auto-deletes after 15s)
- [ ] Both panels revert to `⏸️ Idle`
- [ ] No last_opponent set (footer clean on both panels)

### 6.2 Decline after other accepts
- [ ] Alpha accepts, then Bravo clicks Decline
- [ ] Same result as 6.1

---

## BLOCK 7 — Acceptance Timeout

- [ ] Alpha and Bravo get a match proposal
- [ ] **Do not click anything** for 2 minutes
- [ ] Proposal disappears automatically
- [ ] Cancellation message appears: "acceptance timeout (2 min)"
- [ ] Both panels revert to `⏸️ Idle`

> ⚠️ This test takes 2 minutes. Run it last or in parallel with other blocks.

---

## BLOCK 8 — Parallel Matches

- [ ] Register 4 teams: Alpha, Bravo, Charlie, Delta
- [ ] All 4 click **Ready**
- [ ] Two separate match proposals appear (almost simultaneously)
- [ ] All 4 accept their respective matches
- [ ] Two **MATCH STARTED** broadcasts appear
- [ ] `/match_status` shows 2 active matches
- [ ] End Match 1 → announcement for Match 1 only, Match 2 still active
- [ ] End Match 2 → announcement for Match 2, channel clean

---

## BLOCK 9 — Slash Commands

### 9.1 `/queue_status`
- [ ] With empty queue → "queue is empty"
- [ ] With Alpha queued → shows Alpha
- [ ] Ephemeral (only you see it)

### 9.2 `/match_status`
- [ ] No active matches → "No matches in progress"
- [ ] During active match → shows team names and match ID
- [ ] Ephemeral

### 9.3 `/unregister_team` during match
- [ ] Alpha is IN_MATCH, tries to unregister → ❌ "cannot unregister while a match is active"

---

## BLOCK 10 — Edge Cases

### 10.1 Ready while matched
- [ ] Alpha is in state MATCHED (proposal pending), clicks Ready again → ❌ error

### 10.2 Ready while in match
- [ ] Alpha is IN_MATCH, clicks Ready → ❌ error

### 10.3 Bot restart mid-session
- [ ] Register Alpha and Bravo, both go Ready
- [ ] Stop the bot (`Ctrl+C`)
- [ ] Restart (`python -m daoc_bot`)
- [ ] **Expected:** state is lost (in-memory). Teams need to re-register.
- [ ] Document this behaviour to warn participants at the event

### 10.4 Rapid clicking
- [ ] Click **Ready** 5 times quickly → only one queue entry, no duplicates
- [ ] `/queue_status` confirms only 1 entry for that team

---

## Final Smoke Test

Run immediately before the event starts:

```
python simulate.py
```

All scenarios must pass. If any fail, do not start the event until fixed.

---

## Notes

| Issue found | Steps to reproduce | Fixed? |
|-------------|--------------------|--------|
|             |                    |        |
|             |                    |        |
