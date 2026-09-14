-- Temporary testing fixture installed in Edit, removed after the run.
-- Runs ContentProvider in the actual gameplay client, not MCP's private VM.
local Players = game:GetService('Players')
local HttpService = game:GetService('HttpService')
local provider = game:GetService('ContentProvider')
local root = Instance.new('Folder')
root.Name = '_RobloxAssetVerify'
local request, response = Instance.new('StringValue'), Instance.new('StringValue')
request.Name, response.Name = 'Request', 'Response'
request.Parent, response.Parent = root, root
root.Parent = Players.LocalPlayer:WaitForChild('PlayerGui')
local busy = false
request.Changed:Connect(function(encoded)
	if busy then
		return
	end
	busy = true
	local ok, result = pcall(function()
		local input = HttpService:JSONDecode(encoded)
		assert(tostring(game.PlaceId) == input.destination.place_id, 'Wrong client place')
		assert(tostring(game.GameId) == input.destination.universe_id, 'Wrong client universe')
		assert(#input.entries <= 8, 'Oversized verification batch')
		local rows = {}
		for _, entry in ipairs(input.entries) do
			local row = { key = entry.key, passed = false, content_failures = {}, statuses = {}, context = 'LocalScript/typedInstances' }
			local objects, meshIds = {}, {}
			for _, id in ipairs(entry.mesh_ids or {}) do
				meshIds[id] = true
			end
			for _, id in ipairs(entry.content_ids) do
				local object
				if meshIds[id] then
					object = Instance.new('SpecialMesh')
					object.MeshType = Enum.MeshType.FileMesh
					object.MeshId = id
				else
					object = Instance.new('ImageLabel')
					object.Image = id
				end
				table.insert(objects, object)
			end
			local done, expired = false, false
			task.spawn(function()
				local loaded, failure = pcall(function()
					provider:PreloadAsync(objects, function(id, status)
						if expired then
							return
						end
						table.insert(row.statuses, { id = id, status = status.Name })
						if status ~= Enum.AssetFetchStatus.Success then
							table.insert(row.content_failures, { id = id, status = status.Name })
						end
					end)
					if expired then
						return
					end
					for _, id in ipairs(entry.content_ids) do
						local status = provider:GetAssetFetchStatus(id)
						if status ~= Enum.AssetFetchStatus.Success then
							table.insert(row.content_failures, { id = id, status = status.Name })
						end
					end
					row.passed = #row.content_failures == 0
				end)
				if not expired and not loaded then
					row.error = tostring(failure):sub(1, 500)
				end
				done = true
			end)
			local deadline = os.clock() + 12
			repeat
				task.wait(0.05)
			until done or os.clock() >= deadline
			if not done then
				expired = true
				row.error = 'Gameplay client content preload deadline exceeded'
			end
			for _, object in ipairs(objects) do
				object:Destroy()
			end
			table.insert(rows, row)
		end
		return { id = input.id, rows = rows }
	end)
	busy = false
	response.Value = HttpService:JSONEncode(ok and result or { error = tostring(result) })
end)
