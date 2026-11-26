-- | Which gate a shell command meets, judged the same way by both hosts (kernel/BOUNDARY.md,
-- | *Rulings*: the gate classifier). One simple command in, as the host's parser read it (Claude
-- | Code's shellparse, OpenCode's own scan): its words, quotes undone, and the files its redirects
-- | write. Only a command's own words count, never what its text mentions (#1364): a heredoc's body
-- | or a quoted argument is data, and the host never hands it in as words. The host keeps the
-- | parsing, the paths (cd's, -C's), the refusals' words and the dialogs.
module Kernel.Gate (gateOf) where

import Prelude

import Control.Alt ((<|>))
import Data.Array as A
import Data.Int (toNumber)
import Data.Maybe (Maybe(..), fromMaybe, isJust, maybe)
import Data.String.CodeUnits as S
import Data.String.Common (split, toLower, toUpper)
import Data.String.Pattern (Pattern(..))

type Write = { to :: String, names :: Array String }

type Send =
  { slug :: String
  , title :: String
  , titled :: Boolean
  , bodyFile :: String
  , filed :: Boolean
  , draft :: Boolean
  , anyway :: Boolean
  , lack :: String
  }

-- | `at`: where the command's own word is, past `VAR=x`, the reserved words and the wrappers
-- | (`sudo`, `xargs`, `timeout 60`: #560, #1005); `word`: that word as the system finds it (its
-- | name, lowercased: macOS finds `GIT` as git). `gate`, closed: `cd` (cd or pushd; `args`, its
-- | target, none for home), `send` (wb patch send; `send`, its arguments read), `outward` (a gh
-- | form that writes, a gh api call that writes, a git push; `dirs`, git's -C), `route` (wb route;
-- | `args`, as `wb route --dry-run --json` takes them), `start` (wb start with a --model, any
-- | abbreviation; `args`, `--start` and the start's), `settings` (wb settings set, #1287; `args`,
-- | `--settings` and the row's), or "" (none of them). `delegates`: it hands work out (`wb start`,
-- | `next`, …, #409). `writes`: the files it writes, its redirects', tee's, dd's of= and a copy's
-- | destination (`names`: the sources, whose names it takes in a directory).
gateOf
  :: { words :: Array String, writes :: Array String }
  -> { at :: Number
     , word :: String
     , gate :: String
     , delegates :: Boolean
     , args :: Array String
     , dirs :: Array String
     , writes :: Array { to :: String, names :: Array String }
     , send ::
         { slug :: String
         , title :: String
         , titled :: Boolean
         , bodyFile :: String
         , filed :: Boolean
         , draft :: Boolean
         , anyway :: Boolean
         , lack :: String
         }
     }
gateOf { words, writes } =
  { at: toNumber i
  , word: cmd
  , gate: v.gate
  , delegates: (raw == "wb" || isJust (S.stripSuffix (Pattern "/wb") raw))
      && A.elem (fromMaybe "" (A.index words (i + 1))) handsOut
  , args: v.args
  , dirs: v.dirs
  , writes: map (\to -> { to, names: [] }) writes <> writesOf cmd rest
  , send: if v.gate == "send" then sendOf v.args else noSend
  }
  where
  i = commandAt words
  raw = fromMaybe "" (A.index words i)
  cmd = wordOf raw
  rest = A.drop (i + 1) words
  v = verdict cmd rest (wbArgs cmd rest)

verdict :: String -> Array String -> Maybe (Array String) -> { gate :: String, args :: Array String, dirs :: Array String }
verdict cmd rest wb
  | cmd == "cd" || cmd == "pushd" = { gate: "cd", args: maybe [] pure (cdTarget rest), dirs: [] }
  | cmd == "gh" = if ghWrites rest then outward [] else none
  | cmd == "git" = let g = gitPush rest in if g.push then outward g.dirs else none
  | Just w <- wb, A.take 2 w == [ "patch", "send" ] = { gate: "send", args: A.drop 2 w, dirs: [] }
  | Just w <- wb, A.index w 0 == Just "route" = { gate: "route", args: A.drop 1 w, dirs: [] }
  | Just w <- wb, A.index w 0 == Just "start" && A.any (startsWith "--m") w =
      { gate: "start", args: [ "--start" ] <> A.drop 1 w, dirs: [] }
  | Just w <- wb, A.index w 0 == Just "settings", Just s <- settingsSet w =
      { gate: "settings", args: [ "--settings" ] <> A.slice 1 s w <> A.drop (s + 1) w, dirs: [] }
  | otherwise = none

none :: { gate :: String, args :: Array String, dirs :: Array String }
none = { gate: "", args: [], dirs: [] }

outward :: Array String -> { gate :: String, args :: Array String, dirs :: Array String }
outward dirs = { gate: "outward", args: [], dirs }

startsWith :: String -> String -> Boolean
startsWith p s = isJust (S.stripPrefix (Pattern p) s)

-- | A command word as the system finds it: its name, lowercased
wordOf :: String -> String
wordOf w = toLower (fromMaybe "" (A.last (split (Pattern "/") w)))

-- | The commands that hand work out: run as a command, not merely written down (#409, #560)
handsOut :: Array String
handsOut = [ "start", "next", "feedback", "steer", "answer", "resume" ]

reserved :: Array String
reserved = [ "if", "then", "elif", "else", "while", "until", "do", "!", "{" ]

-- | A wrapper that runs the rest: the options of it that take a value, and the words it takes
-- | before the command (timeout's duration). `sudo git push` and `xargs -I{} git push` hid the
-- | command from the consent gate (#1005).
prefixOf :: String -> Maybe { valued :: Array String, positional :: Int }
prefixOf = case _ of
  "env" -> wraps [ "-u", "-C", "-S", "--unset", "--chdir", "--split-string" ] 0
  "time" -> wraps [ "-f", "-o", "--format", "--output" ] 0
  "nohup" -> wraps [] 0
  "exec" -> wraps [ "-a" ] 0
  "command" -> wraps [] 0
  "timeout" -> wraps [ "-s", "-k", "--signal", "--kill-after" ] 1
  "nice" -> wraps [ "-n", "--adjustment" ] 0
  "ionice" -> wraps [ "-c", "-n", "--class", "--classdata" ] 0
  "stdbuf" -> wraps [ "-i", "-o", "-e", "--input", "--output", "--error" ] 0
  "sudo" -> wraps
    [ "-u", "-g", "-h", "-p", "-C", "-U", "-r", "-t", "-D", "-R", "-T", "--user", "--group", "--host"
    , "--prompt", "--close-from", "--other-user", "--role", "--type", "--chdir", "--chroot", "--command-timeout"
    ]
    0
  "doas" -> wraps [ "-u", "-C" ] 0
  "xargs" -> wraps
    [ "-I", "-L", "-l", "-n", "-P", "-s", "-d", "-E", "-e", "-a", "--max-args", "--max-procs", "--max-lines"
    , "--max-chars", "--delimiter", "--arg-file", "--eof", "--replace", "--process-slot-var"
    ]
    0
  _ -> Nothing
  where
  wraps valued positional = Just { valued, positional }

-- | `VAR=x`: a name, then `=`
assigns :: String -> Boolean
assigns w = case S.indexOf (Pattern "=") w of
  Just k | k > 0 ->
    let
      cs = S.toCharArray (S.take k w)
    in
      maybe false (\c -> c == '_' || letter c) (A.head cs) && A.all (\c -> c == '_' || letter c || digit c) cs
  _ -> false
  where
  letter c = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
  digit c = c >= '0' && c <= '9'

commandAt :: Array String -> Int
commandAt words = go 0
  where
  go i =
    let
      w = fromMaybe "" (A.index words i)
    in
      if assigns w || A.elem w reserved then go (i + 1)
      else case prefixOf w of
        Nothing -> i
        Just p -> go (opts p.valued p.positional (i + 1))
  opts valued positional i = case A.index words i of
    Nothing -> i
    Just o
      | o == "--" -> i + 1
      | startsWith "-" o && S.length o > 1 -> opts valued positional (i + if A.elem o valued then 2 else 1)
      | positional > 0 -> opts valued (positional - 1) (i + 1)
      | otherwise -> i

-- | `python3`, `python3.11`, `python`
python :: String -> Boolean
python cmd = case S.stripPrefix (Pattern "python") cmd of
  Just r -> A.all (\c -> c == '.' || (c >= '0' && c <= '9')) (S.toCharArray r)
  Nothing -> false

-- | The words wb gets when this command runs it: `wb` or a path to it, or Python running a `…/wb`
-- | or `-m workbench…` (`python3 <source>/bin/wb patch send` was no send: the review of #1005)
wbArgs :: String -> Array String -> Maybe (Array String)
wbArgs cmd args
  | cmd == "wb" = Just args
  | not (python cmd) = Nothing
  | otherwise = go 0
  where
  go j = case A.index args j of
    Nothing -> Nothing
    Just a
      | startsWith "-m" a ->
          let
            named = if a == "-m" then fromMaybe "" (A.index args (j + 1)) else S.drop 2 a
          in
            if startsWith "workbench" named then Just (A.drop (j + if a == "-m" then 2 else 1) args) else Nothing
      | a == "-X" || a == "-W" -> go (j + 2)
      | not (startsWith "-" a) -> if wordOf a == "wb" then Just (A.drop (j + 1) args) else Nothing
      | otherwise -> go (j + 1)

-- | The gh forms that write; `gh api` writes with a method other than GET, or with fields (POST by
-- | default; with an explicit GET gh sends them as the query)
ghWrites :: Array String -> Boolean
ghWrites args = case pos 0 [] of
  [ "api", _ ] -> apiWrites args
  [ "api" ] -> apiWrites args
  [ group, sub ] -> A.elem sub (writesIn group)
  _ -> false
  where
  pos j got
    | A.length got >= 2 = got
    | otherwise = case A.index args j of
        Nothing -> got
        Just a
          | a == "-R" || a == "--repo" -> pos (j + 2) got
          | not (startsWith "-" a) -> pos (j + 1) (A.snoc got (toLower a))
          | otherwise -> pos (j + 1) got
  writesIn = case _ of
    "pr" -> [ "create", "comment", "edit", "merge", "review", "close", "reopen", "ready", "update-branch", "lock", "unlock" ]
    "issue" -> [ "create", "comment", "edit", "close", "reopen", "delete", "lock", "unlock", "transfer", "pin", "unpin", "develop" ]
    "repo" -> [ "fork", "edit", "delete", "rename", "archive", "unarchive", "sync" ]
    "workflow" -> [ "run", "enable", "disable" ]
    "run" -> [ "rerun", "cancel", "delete" ]
    "release" -> [ "create", "edit", "delete", "upload", "delete-asset" ]
    "label" -> [ "create", "edit", "delete", "clone" ]
    _ -> []

apiWrites :: Array String -> Boolean
apiWrites args = go 0 "" false
  where
  go j method fields = case A.index args j of
    Nothing -> if method /= "" then method /= "GET" && method /= "HEAD" else fields
    Just a -> case methodOf a of
      Just m | m /= "" -> go (j + 1) (toUpper m) fields
      Just _ -> go (j + 2) (toUpper (fromMaybe "" (A.index args (j + 1)))) fields
      Nothing -> go (j + 1) method (fields || field a)
  -- `-XPOST`, `-X=POST`, `--method=POST`, or `-X`/`--method` with the method next (""); a value
  -- across a line break is none, as gh's flag parser in the mod's old regex had it
  methodOf a = case S.stripPrefix (Pattern "-X") a of
    Just r -> value r
    Nothing -> case S.stripPrefix (Pattern "--method") a of
      Just r | r == "" || startsWith "=" r -> value r
      _ -> Nothing
  value r =
    let
      v = fromMaybe r (S.stripPrefix (Pattern "=") r)
    in
      if A.any (\b -> S.contains (Pattern b) v) [ "\n", "\r", "\x2028", "\x2029" ] then Nothing else Just v
  field a = startsWith "-f" a || startsWith "-F" a
    || A.any (\f -> a == f || startsWith (f <> "=") a) [ "--field", "--raw-field", "--input" ]

-- | git's push, past its global options, and the directories its -C names
gitPush :: Array String -> { push :: Boolean, dirs :: Array String }
gitPush args = go 0 []
  where
  go j dirs = case A.index args j of
    Just a | startsWith "-" a ->
      go (j + if A.elem a valued then 2 else 1)
        (if a == "-C" then A.snoc dirs (fromMaybe "" (A.index args (j + 1))) else dirs)
    other -> { push: toLower (fromMaybe "" other) == "push", dirs }
  valued = [ "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env" ]

-- | A cd or pushd's target word as written (`cd --` and the flags skipped): none for home, `-` for
-- | the last directory (#1125)
cdTarget :: Array String -> Maybe String
cdTarget rest = case A.elemIndex "--" rest of
  Just k -> A.index rest (k + 1)
  Nothing -> A.find (not <<< flag) rest
  where
  flag w = case S.stripPrefix (Pattern "-") w of
    Just r -> r /= "" && A.all (\c -> c == 'L' || c == 'P' || c == 'e' || c == '@') (S.toCharArray r)
    Nothing -> false

-- | Where `set` is in `wb settings …`'s words: wb takes `settings`' own `--json` before it (the
-- | review of #1336)
settingsSet :: Array String -> Maybe Int
settingsSet wb = case A.findIndex (not <<< startsWith "-") (A.drop 1 wb) of
  Just k | A.index wb (k + 1) == Just "set" -> Just (k + 1)
  _ -> Nothing

-- | What tee, dd and the copy-like commands write: tee's files, dd's of=, a copy's last operand or
-- | its -t directory (a file of each source's name)
writesOf :: String -> Array String -> Array Write
writesOf cmd args
  | cmd == "tee" = map (\to -> { to, names: [] }) (A.filter (not <<< startsWith "-") args)
  | cmd == "dd" = A.mapMaybe (\a -> (\to -> { to, names: [] }) <$> S.stripPrefix (Pattern "of=") a) args
  | A.elem cmd [ "cp", "mv", "install", "ln", "rsync" ] = copyTo args 0 [] Nothing
  | otherwise = []

copyTo :: Array String -> Int -> Array String -> Maybe String -> Array Write
copyTo args j ops dir = case A.index args j of
  Nothing -> done ops
  Just a
    | a == "--" -> done (ops <> A.drop (j + 1) args)
    | a == "-t" || a == "--target-directory" -> copyTo args (j + 2) ops (A.index args (j + 1))
    | Just d <- S.stripPrefix (Pattern "--target-directory=") a -> copyTo args (j + 1) ops (Just d)
    | A.elem a [ "-S", "--suffix", "-m", "--mode", "-o", "--owner", "-g", "--group" ] -> copyTo args (j + 2) ops dir
    | not (startsWith "-" a) -> copyTo args (j + 1) (A.snoc ops a) dir
    | otherwise -> copyTo args (j + 1) ops dir
  where
  done all = case dir of
    Just to -> [ { to, names: all } ]
    Nothing -> case A.unsnoc all of
      Just { init, last } | A.length init > 0 -> [ { to: last, names: init } ]
      _ -> []

-- | wb patch send's own words: its slug, --title and --body-file (the last given, as argparse takes
-- | them; wb refuses abbreviations, so only these spellings count), --draft, --anyway. `lack`, what
-- | keeps the dialog from showing what it sends: `slug`, `title`, `bodyFile`, `shell` (a `$` or a
-- | backtick in --title or --body-file, whose value only the shell knows), or "" (#1005).
sendOf :: Array String -> Send
sendOf rest = done (go 0 { slug: Nothing, title: Nothing, file: Nothing, draft: false, anyway: false })
  where
  go j st = case A.index rest j of
    Nothing -> st
    Just a
      | a == "--" -> st { slug = st.slug <|> A.index rest (j + 1) }
      | a == "--title" -> go (j + 2) st { title = A.index rest (j + 1) }
      | a == "--body-file" -> go (j + 2) st { file = A.index rest (j + 1) }
      | Just t <- S.stripPrefix (Pattern "--title=") a -> go (j + 1) st { title = Just t }
      | Just f <- S.stripPrefix (Pattern "--body-file=") a -> go (j + 1) st { file = Just f }
      | a == "--draft" -> go (j + 1) st { draft = true }
      | a == "--anyway" -> go (j + 1) st { anyway = true }
      | not (startsWith "-" a) -> go (j + 1) st { slug = st.slug <|> Just a }
      | otherwise -> go (j + 1) st
  done st =
    let
      s = fromMaybe "" st.slug
      t = fromMaybe "" st.title
      f = fromMaybe "" st.file
      shell x = S.contains (Pattern "$") x || S.contains (Pattern "`") x
    in
      { slug: s
      , title: t
      , titled: isJust st.title
      , bodyFile: f
      , filed: isJust st.file
      , draft: st.draft
      , anyway: st.anyway
      , lack: if s == "" then "slug"
        else if not (isJust st.title) then "title"
        else if not (isJust st.file) then "bodyFile"
        else if shell t || shell f then "shell"
        else ""
      }

noSend :: Send
noSend = { slug: "", title: "", titled: false, bodyFile: "", filed: false, draft: false, anyway: false, lack: "" }
