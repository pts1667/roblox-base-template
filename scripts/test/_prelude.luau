-- Fresh source loading is independent of gameplay instance construction.
-- Redirect requires inside the private graph, including cross-service imports,
-- so a warmed MCP cache cannot mix current factories with older dependencies.
local testModuleHolders = {}

function cleanupTestModules()
	for _, holder in ipairs(testModuleHolders) do holder:Destroy() end
	table.clear(testModuleHolders)
end

function freshRequire(paths, serviceName)
	local service = game:GetService(serviceName or 'ReplicatedStorage')
	local holder = Instance.new('Folder')
	holder.Name = 'RobloxTestFresh'
	holder.Parent = game.ServerStorage
	table.insert(testModuleHolders, holder)
	local references = Instance.new('Folder')
	references.Name = 'References'
	references.Parent = holder
	local originals, parents, copies = {}, {}, {}
	local function include(parent)
		if not parent or parents[parent] then return end
		local graph = Instance.new('Folder')
		graph.Name = parent.Name
		graph.Parent = holder
		parents[parent] = graph
		for _, source in ipairs(parent:GetChildren()) do
			if source:IsA('ModuleScript') then
				local copy = source:Clone()
				copy.Parent = graph
				copies[source] = copy
				local reference = Instance.new('ObjectValue')
				reference.Name = source:GetFullName()
				reference.Value = copy
				reference.Parent = references
			end
		end
	end
	include(game.ReplicatedStorage)
	include(game.ServerScriptService)
	for _, path in ipairs(paths) do
		local source = service
		for segment in string.gmatch(path, '[^/]+') do
			source = source and source:FindFirstChild(segment)
		end
		assert(source and source:IsA('ModuleScript'), 'freshRequire: missing module ' .. path)
		originals[path] = source
		include(source.Parent)
	end
	local loader = [=[
local engineRequire = require
local sourceGraph = script:FindFirstAncestor('RobloxTestFresh')
local function require(module)
	local reference = sourceGraph.References:FindFirstChild(module:GetFullName())
	return engineRequire(reference and reference.Value or module)
end
]=]
	for source, copy in pairs(copies) do copy.Source = loader .. source.Source end
	local results = {}
	for _, path in ipairs(paths) do results[path] = require(copies[originals[path]]) end
	return results
end
