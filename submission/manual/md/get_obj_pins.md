## get_obj_pins

### JsonConfig
```Json
{
  "briefInfo": "get obj pins",
  "description": "Creates a cluster of pins that match the specified criteria",
  "isAtLeastOneArg": false,
}
```
### Argument

**-no_info**

```Json
{
  "token": ["-no_info"],
  "description": "[-no_info]  (Suppresses warning and error messages if no objects match)",
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
  Think of the patterns parameter as a true regular expression rather than a simple wildcard pattern. Also, it modifies the behavior of the =~ and !~ filter operators to compare with real regular expressions rather than simple wildcard patterns. The -regexp and -exact options are mutually exclusive.
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
  "argType": "float",
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
  "description": "[-filter expression]  (Filters the cluster with the specified expression)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0
}
```


- Description:
  The cluster is sifted using expression values. For any pins that match patterns (or objects), the expression is evaluated based on the pin's attributes. If the expression evaluates to true, the pin is included in the result.
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
  "description": "[-hier]  (Searches for pins level-by-level, relative to the current instance)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Searches for pins level-by-level relative to the current instance. The full name of the object at a particular level must match the patterns. The -hier option and the -of option cannot be used simultaneously.
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
  "description": "[patterns]  (Creates a cluster of pins whose names match the specified patterns)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0,
  "exclusive": 1
}
```


- Description:
  Matches pin names against patterns. The patterns option can include the wildcard characters "*" and "?" or regular expressions, based on the -regexp option. The patterns option can also include clusters of pin types. The patterns and -of options are mutually exclusive and cannot be specified at the same time.
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
  "description": "[-of objects]  (Creates a cluster of pins connected to the specified objects)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0,
  "exclusive": 1
}
```


- Description:
  Creates a cluster of pins connected to the specified objects. Each object is a named cell or net, cell cluster or pin cluster, a named library pin, or a cluster of library pins. Note that library pins can not be combined with other types of objects. The -of and patterns options are mutually exclusive and cannot be specified at the same time. The -hier option is ignored when the -of option is specified.
- Valid Value: list.string
- Default Value: N/A
- Rule: None

- Syntax
```
[-of <*objects*>]
objects := <list.string>
```
**-leaf**

```Json
{
  "token": ["-leaf"],
  "description": "[-leaf]  (Includes only those pins that are on leaf cells connected to the nets specified in the -of option.)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0,
  "bindOption": "-of"
}
```


- Description:
  Includes only those pins that are on leaf cells connected to the nets specified in the -of_objects option. The tool crosses hierarchical boundaries to find pins on leaf cells. You can use this option only if you also use the -of_objects option.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-leaf]
```
### Description

The get_obj_pins command creates a cluster of pins in the current design, relative to the current instance, that match certain criteria. If any of the pins match the pattern or object options and pass the filter (if specified), the command returns a cluster. If there is no matching object, an empty string is returned.

### Example

Scenario : The following example shows how to find pins by matching them with a RE that is case-insensitive.

```
get_obj_pins -nocase -regexp "BUF_1/\\[a-z\\]+"
```

### See Also

None
