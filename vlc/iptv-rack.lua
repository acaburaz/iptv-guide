--[[
 IPTV Guide inside VLC.

 The 300 MB playlist stays in the local index. This dialog shows one page
 of search hits and plays a single channel, so VLC's own playlist never
 holds the file.

 Open it from VLC's View menu (Visa in Swedish) after the file is installed
 to %APPDATA%\vlc\lua\extensions\.
]]

-- VLC scans this file before the string library exists, so keep this check plain.
-- install.bat and install.sh replace the path below.
local APP_DIR = [[__IPTV_RACK_DIR__]]

local PORT = 8765
local PAGE = 40

local dlg = nil
local w_source = nil
local w_path = nil
local w_query = nil
local w_list = nil
local w_status = nil
local w_epg = nil
local w_group = nil
local w_profile = nil
local w_now = nil
local w_next = nil
local profile_map = {}
local offset = 0
local mode = "categories"
local active_group = ""
local category_names = {}
local public_groups = nil
local public_items = {}
local total = 0
local active_job = nil

function descriptor()
  return {
    title = "IPTV Guide",
    version = "1.0",
    author = "IPTV Guide",
    shortdesc = "IPTV Guide",
    description = "Index an M3U outside VLC's playlist, show NOW/NEXT, and play one channel.",
    capabilities = {}
  }
end

function activate()
  if dlg then
    dlg:hide()
  end
  offset = 0
  total = 0
  active_job = nil
  dlg = vlc.dialog("IPTV Guide")
  build_dialog()
  dlg:show()
  set_status("Starting the index...")
  if ensure_server() then
    load_sources(nil)
    load_profiles()
  end
end

function close()
  vlc.deactivate()
end

function deactivate()
  if dlg then
    dlg:hide()
  end
  dlg = nil
end

function guard(fn)
  return function()
    local ok, err = pcall(fn)
    if not ok then
      set_status(tostring(err))
    end
  end
end

function build_dialog()
  dlg:add_label("List", 1, 1, 1, 1)
  w_source = dlg:add_dropdown(2, 1, 2, 1)
  dlg:add_button("Refresh", guard(refresh_clicked), 4, 1, 1, 1)

  dlg:add_label("M3U file", 1, 2, 1, 1)
  w_path = dlg:add_text_input("", 2, 2, 1, 1)
  dlg:add_button("Browse", guard(browse_clicked), 3, 2, 1, 1)
  dlg:add_button("Index", guard(index_clicked), 4, 2, 1, 1)

  dlg:add_label("EPG file", 1, 3, 1, 1)
  w_epg = dlg:add_text_input("", 2, 3, 1, 1)
  dlg:add_button("Browse", guard(browse_epg_clicked), 3, 3, 1, 1)
  dlg:add_button("Load", guard(load_epg_clicked), 4, 3, 1, 1)

  dlg:add_label("Profile", 1, 4, 1, 1)
  w_profile = dlg:add_dropdown(2, 4, 1, 1)
  dlg:add_button("Use", guard(use_profile_clicked), 3, 4, 1, 1)
  dlg:add_button("Save", guard(save_profile_clicked), 4, 4, 1, 1)

  dlg:add_label("Search", 1, 5, 1, 1)
  w_query = dlg:add_text_input("", 2, 5, 2, 1)
  dlg:add_button("Search", guard(search_clicked), 4, 5, 1, 1)

  dlg:add_label("Browse", 1, 6, 1, 1)
  dlg:add_button("Categories", guard(categories_clicked), 2, 6, 1, 1)
  dlg:add_button("Open", guard(open_clicked), 3, 6, 1, 1)
  dlg:add_button("Public", guard(public_clicked), 4, 6, 1, 1)

  w_list = dlg:add_list(1, 7, 4, 5)
  w_now = dlg:add_label("NOW: -", 1, 12, 4, 1)
  w_next = dlg:add_label("NEXT: -", 1, 13, 4, 1)
  w_status = dlg:add_label(" ", 1, 14, 4, 1)
  dlg:add_button("Play", guard(play_clicked), 1, 15, 1, 1)
  dlg:add_button("Previous", guard(prev_clicked), 2, 15, 1, 1)
  dlg:add_button("Next", guard(next_clicked), 3, 15, 1, 1)
  dlg:add_button("Guide", guard(guide_clicked), 4, 15, 1, 1)
end

function set_status(text)
  if w_status then
    w_status:set_text(text)
  end
  if dlg then
    dlg:update()
  end
end

function trim(text)
  return (string.gsub(text or "", "^%s*(.-)%s*$", "%1"))
end

function clean_path(text)
  text = trim(text)
  if string.sub(text, 1, 1) == '"' and string.sub(text, -1) == '"' then
    text = string.sub(text, 2, -2)
  end
  return text
end

function commas(value)
  local text = tostring(math.floor(tonumber(value) or 0))
  while true do
    local replaced
    text, replaced = string.gsub(text, "^(-?%d+)(%d%d%d)", "%1,%2")
    if replaced == 0 then
      break
    end
  end
  return text
end

function urlencode(text)
  return (string.gsub(text, "([^%w%-_%.~])", function(char)
    return string.format("%%%02X", string.byte(char))
  end))
end

function pause_ms(ms)
  if vlc.misc and vlc.misc.mwait and vlc.misc.mdate then
    local ok = pcall(function()
      vlc.misc.mwait(vlc.misc.mdate() + ms * 1000)
    end)
    if ok then
      return true
    end
  end
  local seconds = math.max(1, math.floor((ms or 500) / 1000))
  local deadline = os.time() + seconds
  while os.time() < deadline do
  end
  return true
end

function read_port_file()
  local open_fn = io and io.open
  if not open_fn and vlc.io then
    open_fn = vlc.io.open
  end
  if not open_fn then
    return nil
  end
  local handle = open_fn(APP_DIR .. "\\data\\port.txt", "r")
  if not handle then
    return nil
  end
  local line = handle:read("*l")
  handle:close()
  return tonumber(line)
end

function http_get(path)
  if not vlc.net or not vlc.net.connect_tcp then
    return nil, nil
  end
  local fd = vlc.net.connect_tcp("127.0.0.1", PORT)
  if not fd or fd < 0 then
    return nil, nil
  end
  local request = table.concat({
    "GET " .. path .. " HTTP/1.1",
    "Host: 127.0.0.1:" .. tostring(PORT),
    "Connection: close",
    "Accept: application/json",
    "",
    ""
  }, "\r\n")
  vlc.net.send(fd, request)
  local raw = ""
  local pollfds = {}
  pollfds[fd] = vlc.net.POLLIN
  while #raw < 4000000 do
    if vlc.net.poll then
      vlc.net.poll(pollfds)
    end
    local chunk = vlc.net.recv(fd, 65536)
    if not chunk or chunk == "" then
      break
    end
    raw = raw .. chunk
    local length = tonumber(raw:match("Content%-Length:%s*(%d+)"))
    local body = raw:match("\r\n\r\n(.*)")
    if length and body and #body >= length then
      break
    end
  end
  if vlc.net.close then
    vlc.net.close(fd)
  end
  local status = tonumber(raw:match("^HTTP/%d%.%d (%d+)"))
  local body = raw:match("\r\n\r\n(.*)") or ""
  local length = tonumber(raw:match("Content%-Length:%s*(%d+)"))
  if length and #body > length then
    body = string.sub(body, 1, length)
  end
  return status, body
end

function ping()
  local status, body = http_get("/api/ext/ping")
  return status == 200 and body and string.find(body, '"version"', 1, true) ~= nil
end

function ensure_server()
  PORT = 8765
  if ping() then
    return true
  end
  local alt = read_port_file()
  if alt and alt ~= PORT then
    PORT = alt
    if ping() then
      return true
    end
  end
  PORT = 8765
  local status = http_get("/api/sources")
  if status then
    set_status("An older index is still running. Close that console, then click Browse.")
    return false
  end
  if not os or not os.execute then
    set_status("Python 3 has to be on PATH. Install it, then open this panel again.")
    return false
  end
  os.execute('start "IPTV Guide" /MIN pythonw "' .. APP_DIR .. '\\app.py" --no-browser --port 8765')
  local tries = 0
  while tries < 8 do
    pause_ms(500)
    local file_port = read_port_file()
    if file_port then
      PORT = file_port
    end
    if ping() then
      return true
    end
    tries = tries + 1
  end
  set_status("Could not start the index. Install Python 3, quit VLC, and open this panel again.")
  return false
end

function json_decode(text)
  if not text or text == "" then
    return nil
  end
  local ok, value = pcall(json_parse, text)
  if not ok then
    return nil
  end
  return value
end

function json_parse(text)
  local i = 1
  local n = #text

  local function peek()
    return string.sub(text, i, i)
  end

  local function skip()
    while i <= n do
      local char = peek()
      if char == " " or char == "\n" or char == "\r" or char == "\t" then
        i = i + 1
      else
        return
      end
    end
  end

  local parse_value

  local function parse_string()
    i = i + 1
    local out = {}
    while i <= n do
      local char = string.sub(text, i, i)
      if char == '"' then
        i = i + 1
        return table.concat(out)
      end
      if char == "\\" then
        local esc = string.sub(text, i + 1, i + 1)
        if esc == '"' or esc == "\\" or esc == "/" then
          out[#out + 1] = esc
        elseif esc == "n" then
          out[#out + 1] = "\n"
        elseif esc == "r" then
          out[#out + 1] = "\r"
        elseif esc == "t" then
          out[#out + 1] = "\t"
        elseif esc == "u" then
          local hex = string.sub(text, i + 2, i + 5)
          local code = tonumber(hex, 16) or 63
          if code < 128 then
            out[#out + 1] = string.char(code)
          elseif code < 2048 then
            out[#out + 1] = string.char(192 + math.floor(code / 64), 128 + (code % 64))
          else
            out[#out + 1] = string.char(
              224 + math.floor(code / 4096),
              128 + (math.floor(code / 64) % 64),
              128 + (code % 64)
            )
          end
          i = i + 4
        end
        i = i + 2
      else
        out[#out + 1] = char
        i = i + 1
      end
    end
    error("bad string")
  end

  parse_value = function()
    skip()
    local char = peek()
    if char == '"' then
      return parse_string()
    end
    if char == "{" then
      i = i + 1
      local obj = {}
      skip()
      if peek() == "}" then
        i = i + 1
        return obj
      end
      while true do
        skip()
        if peek() ~= '"' then
          error("bad key")
        end
        local key = parse_string()
        skip()
        if peek() ~= ":" then
          error("bad object")
        end
        i = i + 1
        obj[key] = parse_value()
        skip()
        local mark = peek()
        if mark == "," then
          i = i + 1
        elseif mark == "}" then
          i = i + 1
          return obj
        else
          error("bad object")
        end
      end
    end
    if char == "[" then
      i = i + 1
      local arr = {}
      skip()
      if peek() == "]" then
        i = i + 1
        return arr
      end
      while true do
        arr[#arr + 1] = parse_value()
        skip()
        local mark = peek()
        if mark == "," then
          i = i + 1
        elseif mark == "]" then
          i = i + 1
          return arr
        else
          error("bad array")
        end
      end
    end
    if string.sub(text, i, i + 3) == "true" then
      i = i + 4
      return true
    end
    if string.sub(text, i, i + 4) == "false" then
      i = i + 5
      return false
    end
    if string.sub(text, i, i + 3) == "null" then
      i = i + 4
      return nil
    end
    local start = i
    if char == "-" then
      i = i + 1
    end
    local digits = false
    while string.match(string.sub(text, i, i), "%d") do
      digits = true
      i = i + 1
    end
    if string.sub(text, i, i) == "." then
      i = i + 1
      while string.match(string.sub(text, i, i), "%d") do
        digits = true
        i = i + 1
      end
    end
    if not digits then
      error("bad number")
    end
    return tonumber(string.sub(text, start, i - 1))
  end

  return parse_value()
end

function selected_source()
  if not w_source then
    return 0
  end
  local ok, value = pcall(function()
    return w_source:get_value()
  end)
  if not ok then
    return 0
  end
  return tonumber(value) or 0
end

function load_sources(prefer)
  local status, body = http_get("/api/sources")
  if status ~= 200 then
    set_status("The index did not answer.")
    return
  end
  local data = json_decode(body) or {}
  local sources = data.sources or {}
  pcall(function()
    w_source:clear()
  end)
  if #sources == 0 then
    w_source:add_value("(no list yet)", 0)
    set_status("Click Browse and choose the .m3u file.")
    return
  end
  local best = nil
  local best_count = -1
  for _, source in ipairs(sources) do
    local count = tonumber(source.channel_count) or 0
    local id = math.floor(tonumber(source.id) or 0)
    w_source:add_value(tostring(source.name) .. " (" .. commas(count) .. ")", id)
    if prefer and id == math.floor(tonumber(prefer) or -1) then
      best = id
      best_count = count
    elseif not prefer and count > best_count then
      best = id
      best_count = count
    end
  end
  if best and w_source.set_value then
    pcall(function()
      w_source:set_value(best)
    end)
  end
  set_status(commas(best_count) .. " channels indexed.")
  show_categories("", best)
end

function fill_list(items)
  w_list:clear()
  if not items or #items == 0 then
    w_list:add_value("No matches", 0)
    return
  end
  for _, item in ipairs(items) do
    local name = tostring(item.name or "Untitled")
    local group = tostring(item.group or "")
    local label = name
    if group ~= "" then
      label = name .. "  |  " .. group
    end
    local now = tostring(item.now or "")
    if now ~= "" then
      label = label .. "  |  NOW: " .. now
      local left = tostring(item.left or "")
      if left ~= "" then
        label = label .. " (" .. left .. ")"
      end
    end
    local nxt = tostring(item.next or "")
    if nxt ~= "" then
      label = label .. "  |  NEXT: " .. nxt
    end
    if #label > 180 then
      label = string.sub(label, 1, 180)
    end
    w_list:add_value(label, math.floor(tonumber(item.id) or 0))
  end
end

function run_search()
  local source = selected_source()
  if source == 0 then
    set_status("Index a list first.")
    return
  end
  local query = trim(w_query:get_text())
  local group = active_group or ""
  local path = "/api/channels?source=" .. source
    .. "&offset=" .. offset
    .. "&limit=" .. PAGE
    .. "&q=" .. urlencode(query)
    .. "&group=" .. urlencode(group)
  local status, body = http_get(path)
  if status ~= 200 then
    local err = json_decode(body)
    set_status(err and err.error or "Search failed")
    return
  end
  local page = json_decode(body)
  if not page then
    set_status("Could not read the search result.")
    return
  end
  total = tonumber(page.total) or 0
  mode = "channels"
  attach_guide(page.items)
  fill_list(page.items)
  local ms = tonumber(page.ms) or 0
  local shown = 0
  if page.items then
    shown = #page.items
  end
  if total == 0 then
    set_status(string.format("%.1f ms. No matches.", ms))
    return
  end
  local last = math.min(offset + shown, total)
  set_status(string.format(
    "%.1f ms. %s-%s of %s. Play opens one channel.",
    ms,
    commas(offset + 1),
    commas(last),
    commas(total)
  ))
end

function search_clicked()
  if not ensure_server() then
    return
  end
  if mode == "public" then
    show_public(trim(w_query:get_text()))
    return
  end
  if mode ~= "channels" then
    show_categories(trim(w_query:get_text()))
    return
  end
  offset = 0
  run_search()
end

function show_categories(filter, source_id)
  mode = "categories"
  active_group = ""
  offset = 0
  total = 0
  local source = tonumber(source_id) or selected_source()
  if source == 0 then
    set_status("Index a list first.")
    return
  end
  local status, body = http_get("/api/groups?source=" .. source)
  if status ~= 200 then
    set_status("Could not load categories.")
    return
  end
  local data = json_decode(body) or {}
  fill_categories(data.groups or {}, filter or "")
end

function categories_clicked()
  if not ensure_server() then
    return
  end
  if w_query then
    w_query:set_text("")
  end
  show_categories("")
end

function fill_categories(groups, filter)
  category_names = {}
  w_list:clear()
  local needle = string.lower(filter or "")
  local shown = 0
  for _, group in ipairs(groups) do
    local name = tostring(group.name or "")
    local folded = string.lower(name)
    if needle == "" or string.find(folded, needle, 1, true) then
      shown = shown + 1
      category_names[shown] = name
      local count = commas(group.count or 0)
      w_list:add_value(name .. "  (" .. count .. ")", shown)
    end
  end
  if shown == 0 then
    w_list:add_value("No categories", 0)
    set_status("No categories match.")
    return
  end
  set_status(commas(shown) .. " categories. Select one and click Open. Search filters this list.")
end

function public_clicked()
  if not ensure_server() then
    return
  end
  if w_query then
    w_query:set_text("")
  end
  show_public("")
end

function show_public(filter)
  mode = "public"
  active_group = ""
  offset = 0
  total = 0
  if not public_groups then
    local status, body = http_get("/api/public")
    if status ~= 200 then
      set_status("Could not load the public lists.")
      return
    end
    local data = json_decode(body) or {}
    public_groups = data.groups or {}
  end
  fill_public(public_groups, filter or "")
end

function fill_public(groups, filter)
  public_items = {}
  w_list:clear()
  local needle = string.lower(filter or "")
  local shown = 0
  for _, group in ipairs(groups) do
    local section = tostring(group.label or "")
    for _, item in ipairs(group.items or {}) do
      local name = tostring(item.name or "")
      local label = section .. "  " .. name
      if needle == "" or string.find(string.lower(label), needle, 1, true) then
        shown = shown + 1
        public_items[shown] = item
        local count = tonumber(item.count) or 0
        if count > 0 then
          label = label .. "  (" .. commas(count) .. ")"
        end
        w_list:add_value(label, shown)
      end
    end
  end
  if shown == 0 then
    w_list:add_value("No public lists", 0)
    set_status("No public list matches.")
    return
  end
  set_status(commas(shown) .. " built-in public lists. Select one and click Open.")
end

function open_clicked()
  if mode == "public" then
    local pick = selected_channel()
    local item = public_items[pick or 0]
    if not item or not item.url then
      set_status("Select a public list, then click Open.")
      return
    end
    if not ensure_server() then
      return
    end
    local status, body = http_get(
      "/api/ext/public?url=" .. urlencode(tostring(item.url))
      .. "&name=" .. urlencode(tostring(item.name or "Public"))
    )
    local job = json_decode(body)
    if status ~= 200 or not job or not job.id then
      set_status(job and job.error or "Could not open that public list")
      return
    end
    active_job = tostring(job.id)
    watch_job(active_job)
    return
  end
  if mode ~= "categories" then
    set_status("Click Categories, select one, then Open.")
    return
  end
  local pick = selected_channel()
  local name = category_names[pick or 0]
  if not name or name == "" then
    set_status("Select a category, then click Open.")
    return
  end
  active_group = name
  offset = 0
  mode = "channels"
  if w_query then
    w_query:set_text("")
  end
  run_search()
end

function attach_guide(items)
  if not items or #items == 0 then
    return
  end
  local ids = {}
  for _, item in ipairs(items) do
    local id = math.floor(tonumber(item.id) or 0)
    if id > 0 then
      ids[#ids + 1] = tostring(id)
    end
  end
  if #ids == 0 then
    return
  end
  set_status("Loading the guide for " .. tostring(active_group) .. "...")
  local status, body = http_get("/api/ext/guide?ids=" .. table.concat(ids, ","))
  if status ~= 200 then
    return
  end
  local data = json_decode(body) or {}
  local by_id = {}
  for _, guide in ipairs(data.items or {}) do
    by_id[math.floor(tonumber(guide.id) or 0)] = guide
  end
  for _, item in ipairs(items) do
    local guide = by_id[math.floor(tonumber(item.id) or 0)]
    if guide then
      item.now = guide.now or ""
      item.next = guide.next or ""
      item.left = guide.left or ""
      item.desc = guide.desc or ""
    end
  end
end

function next_clicked()
  if mode ~= "channels" then
    return
  end
  if offset + PAGE < total then
    offset = offset + PAGE
    run_search()
  end
end

function prev_clicked()
  if mode ~= "channels" then
    return
  end
  if offset > 0 then
    offset = offset - PAGE
    if offset < 0 then
      offset = 0
    end
    run_search()
  end
end

function refresh_clicked()
  if ensure_server() then
    load_sources(selected_source())
  end
end

function job_line(job)
  local count = tonumber(job.count) or 0
  if job.kind == "epg" then
    if job.state == "error" then
      return tostring(job.error or "Guide failed")
    end
    if job.state == "done" then
      return "Guide ready for " .. commas(count) .. " channels. Search a programme or click Guide."
    end
    return "Reading guide, " .. commas(count) .. " programmes..."
  end
  if job.state == "error" then
    return tostring(job.error or "Index failed")
  end
  if job.state == "done" then
    return "Indexed " .. commas(count) .. " channels. Search, then Play."
  end
  if job.phase == "search" then
    return "Building search for " .. commas(count) .. " channels..."
  end
  return "Indexing " .. commas(count) .. " channels..."
end

function fetch_job(id)
  local status, body = http_get("/api/jobs/" .. id .. "?wait=1")
  if status ~= 200 then
    return nil
  end
  return json_decode(body)
end

function watch_job(id)
  local guard = 0
  while guard < 900 do
    local job = fetch_job(id)
    if not job then
      set_status("Lost contact with the index.")
      return
    end
    set_status(job_line(job))
    if job.state ~= "running" then
      active_job = nil
      if job.state == "done" and job.kind == "epg" then
        load_profiles()
        set_status(job_line(job))
      elseif job.state == "done" then
        load_sources(job.source_id)
      end
      return
    end
    guard = guard + 1
  end
  set_status("Still working. Click Refresh when the count stops climbing.")
end

function pick_file(kind)
  local open_fn = io and io.popen
  if not open_fn then
    return nil
  end
  local handle = open_fn('powershell -NoProfile -STA -File "' .. APP_DIR .. '\\vlc\\pick-m3u.ps1" -Kind ' .. kind)
  if not handle then
    return nil
  end
  local path = ""
  while true do
    local line = handle:read("*l")
    if not line then
      break
    end
    if trim(line) ~= "" then
      path = trim(line)
    end
  end
  handle:close()
  return path
end

function browse_clicked()
  local path = pick_file("m3u")
  if path == nil then
    set_status("Paste the full path to the .m3u file in M3U file, then click Index.")
    return
  end
  if path == "" then
    set_status("No file chosen.")
    return
  end
  w_path:set_text(path)
  index_clicked()
end

function browse_epg_clicked()
  local path = pick_file("epg")
  if path == nil then
    set_status("Paste the full path to the XMLTV file in EPG file, then click Load.")
    return
  end
  if path == "" then
    set_status("No file chosen.")
    return
  end
  w_epg:set_text(path)
  load_epg_clicked()
end

function file_stem(path)
  local name = string.gsub(path or "", "\\", "/")
  name = string.match(name, "([^/]+)$") or name
  name = string.gsub(name, "%.gz$", "")
  name = string.gsub(name, "%.[^%.]+$", "")
  if name == "" then
    return "Guide"
  end
  return name
end

function load_epg_clicked()
  if not ensure_server() then
    return
  end
  if active_job then
    local running = fetch_job(active_job)
    if running and running.state == "running" then
      watch_job(active_job)
      return
    end
    active_job = nil
  end
  local path = clean_path(w_epg:get_text())
  if path == "" then
    set_status("Click Browse and choose the XMLTV guide.")
    return
  end
  local status, body = http_get("/api/ext/epg?path=" .. urlencode(path))
  local job = json_decode(body)
  if status ~= 200 or not job or not job.id then
    set_status(job and job.error or "Could not load the guide")
    return
  end
  active_job = tostring(job.id)
  watch_job(active_job)
end

function load_profiles()
  if not w_profile then
    return
  end
  profile_map = {}
  pcall(function()
    w_profile:clear()
  end)
  local status, body = http_get("/api/profiles")
  if status ~= 200 then
    w_profile:add_value("(no profiles)", 0)
    return
  end
  local data = json_decode(body) or {}
  local profiles = data.profiles or {}
  if #profiles == 0 then
    w_profile:add_value("(no profiles)", 0)
    return
  end
  for index, profile in ipairs(profiles) do
    profile_map[index] = profile
    w_profile:add_value(tostring(profile.name or "Profile"), index)
  end
end

function selected_profile()
  if not w_profile then
    return nil
  end
  local ok, value = pcall(function()
    return w_profile:get_value()
  end)
  if not ok then
    return nil
  end
  return profile_map[tonumber(value) or 0]
end

function use_profile_clicked()
  local profile = selected_profile()
  if not profile then
    set_status("Save a profile first.")
    return
  end
  if w_path and profile.m3u and profile.m3u ~= "" then
    w_path:set_text(tostring(profile.m3u))
  end
  if w_epg and profile.epg and profile.epg ~= "" then
    w_epg:set_text(tostring(profile.epg))
  end
  set_status("Profile " .. tostring(profile.name) .. " filled in. Click Load for the guide.")
end

function save_profile_clicked()
  if not ensure_server() then
    return
  end
  local m3u = clean_path(w_path:get_text())
  local epg = clean_path(w_epg:get_text())
  local name = file_stem(m3u)
  if name == "Guide" or name == "" then
    name = file_stem(epg)
  end
  local status, body = http_get(
    "/api/ext/profile?name=" .. urlencode(name)
    .. "&m3u=" .. urlencode(m3u)
    .. "&epg=" .. urlencode(epg)
  )
  if status ~= 200 then
    local err = json_decode(body)
    set_status(err and err.error or "Could not save the profile")
    return
  end
  load_profiles()
  set_status("Saved profile " .. name .. ".")
end

function show_guide(item)
  local now = tostring(item.now or "")
  local nxt = tostring(item.next or "")
  if now == "" then
    now = "-"
  end
  if nxt == "" then
    nxt = "-"
  end
  if w_now then
    w_now:set_text("NOW: " .. now)
  end
  if w_next then
    w_next:set_text("NEXT: " .. nxt)
  end
  local line = "NOW: " .. now .. "   NEXT: " .. nxt
  local left = tostring(item.left or "")
  if left ~= "" then
    line = line .. "   " .. left .. " left"
  end
  local desc = tostring(item.desc or "")
  if desc ~= "" then
    line = line .. "   " .. desc
  end
  set_status(line)
end

function guide_clicked()
  local id = selected_channel()
  if not id or id == 0 then
    set_status("Select a channel first.")
    return
  end
  local status, body = http_get("/api/ext/channel?id=" .. math.floor(id))
  local item = json_decode(body)
  if status ~= 200 or not item then
    set_status(item and item.error or "Channel not found")
    return
  end
  show_guide(item)
end

function index_clicked()
  if not ensure_server() then
    return
  end
  if active_job then
    local running = fetch_job(active_job)
    if running and running.state == "running" then
      watch_job(active_job)
      return
    end
    active_job = nil
  end
  local path = clean_path(w_path:get_text())
  if path == "" then
    set_status("Click Browse and choose the .m3u file.")
    return
  end
  local status, body = http_get("/api/ext/index?path=" .. urlencode(path))
  local job = json_decode(body)
  if status ~= 200 or not job or not job.id then
    set_status(job and job.error or "Could not start the index")
    return
  end
  active_job = tostring(job.id)
  watch_job(active_job)
end

function selected_channel()
  if not w_list then
    return 0
  end
  local ok, selection = pcall(function()
    return w_list:get_selection()
  end)
  if not ok or not selection then
    return 0
  end
  for id, _ in pairs(selection) do
    return tonumber(id) or 0
  end
  return 0
end

function play_clicked()
  local id = selected_channel()
  if not id or id == 0 then
    set_status("Select a channel first.")
    return
  end
  local status, body = http_get("/api/ext/channel?id=" .. math.floor(id))
  local item = json_decode(body)
  if status ~= 200 or not item or not item.url then
    set_status(item and item.error or "Channel not found")
    return
  end
  local options = {}
  if type(item.opts) == "table" then
    for _, pair in ipairs(item.opts) do
      if type(pair) == "table" and pair[1] and pair[2] then
        options[#options + 1] = ":" .. tostring(pair[1]) .. "=" .. tostring(pair[2])
      end
    end
  end
  local entry = { path = item.url, name = tostring(item.name or "Channel") }
  if #options > 0 then
    entry.options = options
  end
  vlc.playlist.stop()
  vlc.playlist.clear()
  vlc.playlist.add({ entry })
  vlc.playlist.play()
  http_get("/api/ext/remember?id=" .. math.floor(id))
  show_guide(item)
  set_status("Playing " .. tostring(item.name or "channel"))
end

function stop_clicked()
  vlc.playlist.stop()
  set_status("Stopped")
end
