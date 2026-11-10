## dump_scan_def

### JsonConfig
```Json
{
  "briefInfo": "dump SCAN DEF",
  "description": "Write SCAN DEF to the specified file",
  "isAtLeastOneArg": false,
}
```
### Argument

**-file**

```Json
{
  "token": ["-file"],
  "description": "[-file file_path]  (Specify the SCAN DEF file name to dump)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [1,1],
  "orderNum": 0
}
```


- Description:
  Specify the SCAN DEF file name to dump. If you do not specify the -file option, a new file is created. i.e. './xxx.def', where 'xxx' is the name of SCAN DEF in the db.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
-file <*file_path*>
file_path := <string>
```
**-expand_insts**

```Json
{
  "token": ["-expand_insts"],
  "description": "[-expand_insts insts_list]  (expand insts inter scan info to DEF file)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0
}
```


- Description:
  expand hinsts' inner scanchain information to top scandef when read hinsts' ctl files
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
-expand_insts <*expand_insts*>
expand_insts := <string>
```

### Description

Specify which inst the def should expand.

### Example

Scenario : The following example specifies dump scan def expand inst1 and inst2:

```
dump_scan_def -file scan_chain.def -expand_insts {inst1 inst2}
```

### See Also
