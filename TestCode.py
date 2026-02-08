import math
import numpy as np
from sklearn.linear_model import LinearRegression
import matplotlib.pyplot as plt

# Linear regression model

# Defining variables 

IG_vector = np.zeros(10)
IB_vector = np.zeros(10)

# fit linear regression model 
model = LinearRegression()
model.fit(IG_vector, IB_vector)






