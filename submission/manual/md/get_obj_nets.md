## get_obj_nets

### JsonConfig
```Json
{
  "briefInfo": "get obj nets",
  "description": "Creates a cluster of nets that match the specified criteria",
  "isAtLeastOneArg": false,
}
```
### Argument

**-no_info**

```Json
{
  "token": ["-no_info"],
  "description": "[-no_info]  (Suppresses messages if no objects match)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  If there is no matching object, the warning and error messages are suppressed. Syntax error messages are not suppressed.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-no_info]
```
**-regexp**

```Json
{
  "token": ["-regexp"],
  "description": "[-regexp]  (Views the patterns argument as a regular expression rather than a simple wildcard pattern)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0,
  "exclusive": 2
}
```


- Description:
  Think of the patterns parameter as a true regular expression rather than a simple wildcard pattern. The -regexp and -exact options are mutually exclusive.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-regexp]
```
**-exact**

```Json
{
  "token": ["-exact"],
  "description": "[-exact]  (Considers wildcards to be plain characters, and does not interpret their meaning as wildcards)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0,
  "exclusive": 2
}
```


- Description:
  Simple pattern matching is disabled. Used for searching containing * and ? Wildcard character. The -exact and -regexp options are mutually exclusive.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-exact]
```
**-nocase**

```Json
{
  "token": ["-nocase"],
  "description": "[-nocase]  (Makes matches case-insensitive, both for the patterns argument and for the ==, =~, and !~ filter operators)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  When used in combination with the -regexp option, makes matches case-insensitive.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-nocase]
```
**-filter**

```Json
{
  "token": ["-filter"],
  "description": "[-filter expression]  (Filters the cluster with the value of the expression argument)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0
}
```


- Description:
  The cluster is sifted using expression values. For any nets that match patterns (or objects), the expression is evaluated based on the cell's attributes. If the expression evaluates to true, the net is included in the result.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
[-filter <*expression*>]
expression := <string>
```
**-hier**

```Json
{
  "token": ["-hier"],
  "description": "[-hier]  (Searches for nets level-by-level relative to the current instance)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Searches for nets level-by-level relative to the current instance. The full name of the object at a particular level must match the patterns. The -hier option is ignored when the -of option is specified.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-hier]
```
**patterns**

```Json
{
  "token": [],
  "description": "[patterns]  (Creates a cluster of nets whose names match the specified patterns)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0,
  "exclusive": 1
}
```


- Description:
  Matches net names against patterns. Based on the -regexp option, a pattern can include wildcards "*" and "?" or regular expression. Patterns can also include clusters of net type. The patterns and -of options are mutually exclusive and cannot be specified at the same time.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
<*patterns*>
patterns := <string>
```
**-of**

```Json
{
  "token": ["-of"],
  "description": "[-of objects]  (Creates a cluster of nets connected to the specified objects)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0,
  "exclusive": 1
}
```


- Description:
  Creates a cluster of nets connected to the specified objects. Each object is either a named pin, a pin cluster, a named cell or cell cluster. The -of and patterns options are mutually exclusive and cannot be specified at the same time. The -hier option is ignored when the -of option is specified.
- Valid Value: list.string
- Default Value: N/A
- Rule: None

- Syntax
```
[-of <*objects*>]
objects := <list.string>
```
**-upper**

```Json
{
  "token": ["-upper"],
  "description": "[-upper]  (Specifies this option to find the upper connection net when find net by hpin)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies this option to find the upper connection net when find net by hpin. If neither upper nor lower is specified, the default upper is true.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-upper]
```
**-lower**

```Json
{
  "token": ["-lower"],
  "description": "[-lower]  (Specifies this option to find the lower connection net when find net by hpin)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies this option to find the lower connection net when find net by hpin. If neither upper nor lower is specified, the default upper is true.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-lower]
```
**-segments**

```Json
{
  "token": ["-segments"],
  "description": "[-segments]  (Returns all global segments for given nets)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Modifies the initial search that matches the patterns or -of argument to include all global segments for the matching nets. Global net segments are all the net segments that are physically connected across all hierarchical boundaries.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-segments]
```
### Description

The get_obj_nets command creates a cluster of nets in the current design relative to the current instance matching some condition. If any net matches a pattern or object and passes the filter (if specified), the command returns a cluster. If no object matches the condition, an empty cluster is returned.

If any patterns (or objects) fail to match any objects and the current design is not linked, the design automatically links.

### Example

Scenario 1: The following example shows getting nets using the get nets command.

```
get_obj_nets
```

Scenario 2: The following example shows a net of the same name filtered through -filter.

```
get_obj_nets -filter {name == "G2"}
```

Scenario 3: The following example shows a network with the same name filtered through -filer. If no net object meets the filtering criteria, run the -no_info command to disable the warning message.

```
get_obj_nets -filter {name == "test"} -no_info
```

Scenario 4: The following example shows how to use the upper and lower option.

```
get_obj_nets -of [get_obj_pins H1/D] -upper
get_obj_nets -of [get_obj_pins H1/D]
get_obj_nets -of [get_obj_pins H1/D] -lower
get_obj_nets -of [get_obj_pins H1/D] -upper -lower
```

### See Also

[add_net](#add_net)

[delete_net](#delete_net)
