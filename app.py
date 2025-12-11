"""
Python Code Execution API - Secure Code Runner for AI Agents
Supports: Math, Charts, Data Analysis, ML
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any
import sys
import io
import base64
import traceback
import contextlib
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import sympy as sp
from scipy import stats, optimize, integrate
import json
import warnings
warnings.filterwarnings('ignore')

app = FastAPI(title="Python Code Execution API", version="1.0.0")

# CORS للسماح بالوصول من أي مكان
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CodeRequest(BaseModel):
    code: str
    timeout: Optional[int] = 30
    return_images: Optional[bool] = True

class CodeResponse(BaseModel):
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    images: Optional[list] = None
    execution_time: Optional[float] = None

def capture_plots():
    """التقاط جميع الرسومات البيانية"""
    images = []
    for i in plt.get_fignums():
        fig = plt.figure(i)
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        images.append(f"data:image/png;base64,{img_base64}")
        buf.close()
    plt.close('all')
    return images

def safe_exec(code: str, timeout: int = 30):
    """تشغيل الكود بشكل آمن"""
    
    # إنشاء بيئة محدودة للتنفيذ
    safe_globals = {
        '__builtins__': {
            'print': print,
            'range': range,
            'len': len,
            'int': int,
            'float': float,
            'str': str,
            'list': list,
            'dict': dict,
            'tuple': tuple,
            'set': set,
            'abs': abs,
            'max': max,
            'min': min,
            'sum': sum,
            'round': round,
            'sorted': sorted,
            'enumerate': enumerate,
            'zip': zip,
            'map': map,
            'filter': filter,
            'True': True,
            'False': False,
            'None': None,
        },
        # مكتبات الرياضيات والرسم
        'np': np,
        'numpy': np,
        'plt': plt,
        'matplotlib': matplotlib,
        'pd': pd,
        'pandas': pd,
        'sp': sp,
        'sympy': sp,
        'stats': stats,
        'optimize': optimize,
        'integrate': integrate,
        'math': __import__('math'),
    }
    
    safe_locals = {}
    
    # التقاط المخرجات
    output_capture = io.StringIO()
    
    try:
        with contextlib.redirect_stdout(output_capture):
            with contextlib.redirect_stderr(output_capture):
                exec(code, safe_globals, safe_locals)
        
        output = output_capture.getvalue()
        images = capture_plots()
        
        return {
            'success': True,
            'output': output if output else "Code executed successfully (no output)",
            'images': images,
            'error': None
        }
    
    except Exception as e:
        plt.close('all')
        error_msg = f"{type(e).__name__}: {str(e)}\n\n{traceback.format_exc()}"
        return {
            'success': False,
            'output': output_capture.getvalue(),
            'images': None,
            'error': error_msg
        }
    finally:
        output_capture.close()

@app.get("/")
def root():
    return {
        "message": "Python Code Execution API",
        "version": "1.0.0",
        "endpoints": {
            "/execute": "POST - Execute Python code",
            "/health": "GET - Health check",
            "/libraries": "GET - List available libraries"
        }
    }

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.get("/libraries")
def libraries():
    return {
        "math": ["numpy", "scipy", "sympy"],
        "visualization": ["matplotlib", "seaborn"],
        "data": ["pandas"],
        "available_modules": {
            "numpy": "Numerical computing",
            "matplotlib": "Plotting and visualization",
            "pandas": "Data analysis",
            "sympy": "Symbolic mathematics",
            "scipy": "Scientific computing (stats, optimize, integrate)"
        }
    }

@app.post("/execute", response_model=CodeResponse)
async def execute_code(request: CodeRequest):
    """
    تشغيل كود Python
    
    Example:
    ```python
    {
        "code": "import numpy as np\nimport matplotlib.pyplot as plt\nx = np.linspace(0, 10, 100)\ny = np.sin(x)\nplt.plot(x, y)\nplt.title('Sine Wave')\nplt.show()",
        "timeout": 30,
        "return_images": true
    }
    ```
    """
    
    if not request.code or not request.code.strip():
        raise HTTPException(status_code=400, detail="Code cannot be empty")
    
    # فحص الكود من الأوامر الخطرة
    dangerous_keywords = ['import os', 'import subprocess', '__import__', 'eval(', 'exec(', 
                          'open(', 'file(', 'input(', 'raw_input(']
    
    for keyword in dangerous_keywords:
        if keyword in request.code:
            raise HTTPException(
                status_code=400, 
                detail=f"Dangerous operation detected: {keyword}"
            )
    
    result = safe_exec(request.code, request.timeout)
    
    return CodeResponse(
        success=result['success'],
        output=result['output'],
        error=result['error'],
        images=result['images'] if request.return_images else None
    )

# أمثلة للاختبار
EXAMPLES = {
    "simple_plot": """
import matplotlib.pyplot as plt
import numpy as np

x = np.linspace(0, 2*np.pi, 100)
y = np.sin(x)

plt.figure(figsize=(10, 6))
plt.plot(x, y, 'b-', linewidth=2)
plt.title('Sine Wave', fontsize=16)
plt.xlabel('x')
plt.ylabel('sin(x)')
plt.grid(True)
plt.show()
""",
    
    "mathematical_function": """
import numpy as np
import matplotlib.pyplot as plt

# رسم عدة دوال رياضية
x = np.linspace(-5, 5, 100)

fig, axes = plt.subplots(2, 2, figsize=(12, 10))

# sin and cos
axes[0, 0].plot(x, np.sin(x), label='sin(x)')
axes[0, 0].plot(x, np.cos(x), label='cos(x)')
axes[0, 0].set_title('Trigonometric Functions')
axes[0, 0].legend()
axes[0, 0].grid(True)

# exponential
axes[0, 1].plot(x, np.exp(x))
axes[0, 1].set_title('Exponential: e^x')
axes[0, 1].grid(True)

# polynomial
axes[1, 0].plot(x, x**2, label='x²')
axes[1, 0].plot(x, x**3, label='x³')
axes[1, 0].set_title('Polynomial Functions')
axes[1, 0].legend()
axes[1, 0].grid(True)

# logarithm
x_pos = np.linspace(0.1, 5, 100)
axes[1, 1].plot(x_pos, np.log(x_pos))
axes[1, 1].set_title('Natural Logarithm: ln(x)')
axes[1, 1].grid(True)

plt.tight_layout()
plt.show()
""",

    "symbolic_math": """
import sympy as sp

# حساب التفاضل والتكامل
x = sp.Symbol('x')
f = sp.sin(x) * sp.exp(x)

print("Function:", f)
print("\\nDerivative:", sp.diff(f, x))
print("\\nIntegral:", sp.integrate(f, x))
print("\\nLimit as x->0:", sp.limit(f, x, 0))

# حل المعادلات
eq = sp.Eq(x**2 - 5*x + 6, 0)
solutions = sp.solve(eq, x)
print("\\nSolutions of x² - 5x + 6 = 0:", solutions)
""",

    "data_analysis": """
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# إنشاء بيانات عشوائية
data = pd.DataFrame({
    'x': np.random.randn(100),
    'y': np.random.randn(100),
    'category': np.random.choice(['A', 'B', 'C'], 100)
})

print("Data Statistics:")
print(data.describe())

# رسم بياني
fig, axes = plt.subplots(1, 2, figsize=(12, 5))

# Scatter plot
for cat in data['category'].unique():
    mask = data['category'] == cat
    axes[0].scatter(data[mask]['x'], data[mask]['y'], label=cat, alpha=0.6)
axes[0].set_title('Scatter Plot by Category')
axes[0].legend()
axes[0].grid(True)

# Histogram
data['x'].hist(bins=20, ax=axes[1], edgecolor='black')
axes[1].set_title('Distribution of X')
axes[1].grid(True)

plt.tight_layout()
plt.show()
"""
}

@app.get("/examples")
def get_examples():
    return {
        "examples": list(EXAMPLES.keys()),
        "description": "Get example code snippets",
        "usage": "GET /examples/{example_name}"
    }

@app.get("/examples/{example_name}")
def get_example(example_name: str):
    if example_name not in EXAMPLES:
        raise HTTPException(status_code=404, detail="Example not found")
    return {
        "name": example_name,
        "code": EXAMPLES[example_name]
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
