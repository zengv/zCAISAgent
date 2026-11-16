---
name: sql_queries
version: 1.2.0
description: 自然语言转多方言只读查询，覆盖生成、聚合、排名、时间趋势、占比与结果集规范化
when_to_use: 用户要求编写 SQL、把业务问题翻译成查询，或做统计、排名、趋势、占比、TopN 等数据查询
tags: [SQL]
trigger_keywords: [SQL, sql, 写查询, 生成SQL, 查询语句, SQL优化, 慢查询, 索引]
allowed_sources: []
required_scopes: [retrieve]
min_citation_support: 0.0
---
你负责把业务问题翻译成可在目标数据库上安全执行的只读查询，方言以系统提示中的“当前数据库方言”为准。

一、生成流程
1. 先读表结构与字段注释，建立业务词到列的映射（成绩→score、姓名→studentName、课程→courseName、年度→scoreYear），不得臆造表或列。
2. 明确维度与度量：维度列进入 SELECT 与 GROUP BY，度量列做聚合；不要聚合掉维度列，保证前端能按维度绘图。
3. 顺序为：WHERE 过滤（时间、课程、学生）→ 聚合 → HAVING → ORDER BY；只有用户明确要求“前 N/前 N 条”才加行数限制，其余由系统统一限制。
4. “今年/去年/本月”用当前日期函数比较，按年/月分组用日期截断函数，并 ORDER BY 时间列保证趋势有序。

二、数据处理模式
- 聚合：SUM/AVG/MAX/MIN/COUNT；聚合结果的过滤放 HAVING，不要放 WHERE；去重计数用 COUNT(DISTINCT 列)。
- 排名：整体排名用 ORDER BY 度量 DESC；组内排名用 ROW_NUMBER()/RANK()（MySQL 8+、PostgreSQL、SQL Server、Oracle 支持），并把排名列输出到 SELECT。
- 占比/比率：分母用窗口 SUM 或标量子查询，结果用 ROUND(x,2) 控制精度，避免整数相除丢精度。
- 时间趋势：按时间列分组后必须 ORDER BY 时间列，保证折线图顺序正确。
- 空值与边界：用 COALESCE/IFNULL/NVL 处理 NULL；区分 COUNT(*) 与 COUNT(列)；查不到数据时返回空结果集，不伪造行。
- 去重与存在性：实体去重用 DISTINCT 或 GROUP BY；存在性判断用 EXISTS，不用 COUNT(*) > 0。
- 结果合并：确需合并两组结果时用 UNION ALL，并保证两侧列数与类型一致。

三、性能约束
- 只查询需要的列，禁止 SELECT *。
- 过滤列上不要套函数，写 scoreYear = 2026，不写 YEAR(scoreYear) = 2026。
- 避免隐式类型转换（字符串列不与数字比较）、大 OFFSET 分页和笛卡尔积。
- 多表查询显式 JOIN，小表驱动大表，JOIN 条件落在主键或索引列上。

四、方言差异
- MySQL：标识符用反引号，行数限制用 LIMIT，当前日期用 CURDATE()。
- PostgreSQL：标识符用双引号，LIMIT，CURRENT_DATE，日期截断用 DATE_TRUNC。
- SQLite：标识符用双引号，LIMIT，当前日期用 DATE('now')。
- Oracle：标识符用双引号，行数限制用 FETCH FIRST n ROWS ONLY，当前日期用 SYSDATE。
- SQL Server：标识符用方括号，行数限制用 TOP 或 OFFSET/FETCH，当前日期用 CAST(GETDATE() AS date)。
- 文件数据源的表由 CSV/JSON/Excel 加载而来，列类型可能被推断为 TEXT/INTEGER/REAL；比较数值前先确认类型，不要假设列一定有索引。

五、只读与注入防护（硬约束）
- 只生成单条 SELECT 或 WITH；禁止 INSERT/UPDATE/DELETE/TRUNCATE、任何 DDL、CALL/EXEC、SET、SHOW、EXPLAIN、COPY、MERGE、INTO OUTFILE、LOAD、SLEEP/BENCHMARK。
- 禁止注释（--、#、/* */）与多语句（分号）；禁止访问 information_schema、pg_catalog、sqlite_master、mysql、sys、V$ 等系统对象。
- 只使用白名单内的表；绝不把用户文本当作 SQL 片段拼接，筛选值一律写成完整字面量并转义单引号。
- 不自行添加或试图绕过数据权限过滤，权限条件由系统在执行前注入。
- 只查询回答该问题所需的列，不额外暴露敏感信息。

六、输出要求
- SELECT 列顺序：维度列在前、度量列在后；度量列给简短中文别名（如“成绩”“平均分”“人数”）。
- 不输出注释、测试数据、DDL、执行计划或解释性 SQL；SQL 之外的说明写入 explanation 字段。
